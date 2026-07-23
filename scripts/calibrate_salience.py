#!/usr/bin/env python3
"""Offline calibration harness for LCMA salience recalibration (task e7d8ef60).

Runs **read-only against a copy of a prod/staging ``stats.db``** — never the live
instance — and helps pick the shipped defaults for the salience floor and the usage
signal. It imports the same pure helpers the server uses
(:mod:`lithos.lcma.salience`), so the numbers reflect exactly what runs in production.

It answers two questions:

1. **Floor** — for each candidate floor, what does the salience distribution look like
   after the one-time ``max(salience, floor)`` backfill (mean, % ≤ 0.30, spread)?
2. **Usage signal** — for each candidate ``usage_score`` parameter set, what is the
   resulting distribution, and how well does it *separate* nodes that were genuinely
   used (cited, or frequently retrieved) from cold ones? Configs are ranked by a
   rank-based AUC so a good spread that also tracks real usage floats to the top.

Ground truth is thin while explicit feedback is near-empty, so the ranking falls back
from citation-AUC to retrieval-activity-AUC (with a warning) when there are too few
cited nodes — exactly the regime the recalibration was written for.

Usage:
    python scripts/calibrate_salience.py /path/to/stats-copy.db
    python scripts/calibrate_salience.py stats.db --floors 0.25,0.3,0.35 --top 8
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from lithos.lcma.salience import usage_score

# Candidate usage-signal parameter sets, expanded from these axes by default.
_FREQ_RECENCY_SPLITS: tuple[tuple[float, float], ...] = (
    (0.6, 0.4),
    (0.5, 0.5),
    (0.7, 0.3),
    (0.4, 0.6),
)
_HALFLIVES: tuple[float, ...] = (7.0, 14.0, 30.0)
_NORM_KS: tuple[float, ...] = (10.0, 20.0, 50.0)
_DEFAULT_FLOORS: tuple[float, ...] = (0.2, 0.25, 0.3, 0.35, 0.4)


@dataclass(frozen=True)
class NodeRow:
    """The node_stats fields the calibration needs."""

    salience: float
    retrieval_count: int
    cited_count: int
    days_since_use: float | None


@dataclass(frozen=True)
class UsageConfig:
    freq_weight: float
    recency_weight: float
    recency_halflife_days: float
    freq_norm_k: float

    def label(self) -> str:
        return (
            f"f={self.freq_weight:.1f} r={self.recency_weight:.1f} "
            f"hl={self.recency_halflife_days:g} k={self.freq_norm_k:g}"
        )


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def load_nodes(db_path: str) -> list[NodeRow]:
    """Read node_stats from a stats.db copy (read-only).

    Recency is measured relative to the newest activity in the snapshot rather than
    wall-clock now, so the result is deterministic and independent of when the harness
    runs.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT salience, retrieval_count, cited_count, "
            "last_used_at, last_retrieved_at FROM node_stats"
        ).fetchall()
    finally:
        conn.close()

    times: list[datetime] = []
    parsed: list[tuple[sqlite3.Row, datetime | None]] = []
    for row in rows:
        ts = _parse_ts(row["last_used_at"]) or _parse_ts(row["last_retrieved_at"])
        if ts is not None:
            times.append(ts)
        parsed.append((row, ts))

    reference = max(times) if times else datetime.now(UTC)
    nodes: list[NodeRow] = []
    for row, ts in parsed:
        days = None if ts is None else max(0.0, (reference - ts).total_seconds() / 86400.0)
        nodes.append(
            NodeRow(
                salience=float(row["salience"]),
                retrieval_count=int(row["retrieval_count"]),
                cited_count=int(row["cited_count"]),
                days_since_use=days,
            )
        )
    return nodes


def auc(positives: Sequence[float], negatives: Sequence[float]) -> float:
    """Probability a random positive outranks a random negative (ties count 0.5).

    Returns 0.5 (uninformative) when either group is empty. O(n log n) via rank sums
    (Mann-Whitney U), so it scales to the whole corpus.
    """
    if not positives or not negatives:
        return 0.5
    labelled = [(v, 1) for v in positives] + [(v, 0) for v in negatives]
    labelled.sort(key=lambda t: t[0])
    # Average ranks over ties.
    ranks = [0.0] * len(labelled)
    i = 0
    while i < len(labelled):
        j = i
        while j + 1 < len(labelled) and labelled[j + 1][0] == labelled[i][0]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # 1-based average rank across the tie block
        for k in range(i, j + 1):
            ranks[k] = avg_rank
        i = j + 1
    rank_sum_pos = sum(r for r, (_, lbl) in zip(ranks, labelled, strict=True) if lbl == 1)
    n_pos = len(positives)
    n_neg = len(negatives)
    u = rank_sum_pos - n_pos * (n_pos + 1) / 2
    return u / (n_pos * n_neg)


def _fraction(values: Sequence[float], predicate: object) -> float:
    if not values:
        return 0.0
    assert callable(predicate)
    return sum(1 for v in values if predicate(v)) / len(values)


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, int(q * len(sorted_values))))
    return sorted_values[idx]


def floor_report(nodes: Sequence[NodeRow], floors: Sequence[float]) -> list[str]:
    """One row per candidate floor: the salience distribution after backfill."""
    out = ["Floor backfill — salience distribution after max(salience, floor):"]
    out.append(f"  {'floor':>6}  {'mean':>6}  {'<=0.30':>7}  {'std':>6}  {'p50':>6}  {'p90':>6}")
    for floor in floors:
        lifted = [max(n.salience, floor) for n in nodes]
        s = sorted(lifted)
        mean = statistics.fmean(lifted) if lifted else 0.0
        std = statistics.pstdev(lifted) if len(lifted) > 1 else 0.0
        frac = _fraction(lifted, lambda v: v <= 0.30)
        out.append(
            f"  {floor:>6.2f}  {mean:>6.3f}  {frac * 100:>6.1f}%  "
            f"{std:>6.3f}  {_percentile(s, 0.5):>6.3f}  {_percentile(s, 0.9):>6.3f}"
        )
    return out


@dataclass(frozen=True)
class UsageResult:
    config: UsageConfig
    mean: float
    std: float
    frac_zero: float
    frac_saturated: float
    cited_auc: float
    activity_auc: float
    ranking_auc: float
    ranked_by: str


def _score_all(nodes: Sequence[NodeRow], cfg: UsageConfig) -> list[float]:
    return [
        usage_score(
            n.retrieval_count,
            n.days_since_use,
            freq_weight=cfg.freq_weight,
            recency_weight=cfg.recency_weight,
            recency_halflife_days=cfg.recency_halflife_days,
            freq_norm_k=cfg.freq_norm_k,
        )
        for n in nodes
    ]


def evaluate_usage(nodes: Sequence[NodeRow], configs: Sequence[UsageConfig]) -> list[UsageResult]:
    """Score every node under each config and rank configs by separation.

    Primary signal is citation-AUC (independent of retrieval); when there are too few
    cited nodes to be meaningful it falls back to retrieval-activity AUC.
    """
    retrievals = [n.retrieval_count for n in nodes]
    active_ret = [r for r in retrievals if r > 0]
    ret_threshold = statistics.median(active_ret) if active_ret else 1
    cited_pos = sum(1 for n in nodes if n.cited_count > 0)
    use_cited = cited_pos >= 10 and (len(nodes) - cited_pos) >= 10

    results: list[UsageResult] = []
    for cfg in configs:
        scores = _score_all(nodes, cfg)
        cited_p = [s for s, n in zip(scores, nodes, strict=True) if n.cited_count > 0]
        cited_n = [s for s, n in zip(scores, nodes, strict=True) if n.cited_count == 0]
        act_p = [
            s
            for s, n in zip(scores, nodes, strict=True)
            if n.retrieval_count >= ret_threshold and n.retrieval_count > 0
        ]
        act_n = [s for s, n in zip(scores, nodes, strict=True) if n.retrieval_count == 0]
        cited_auc = auc(cited_p, cited_n)
        activity_auc = auc(act_p, act_n)
        ranking_auc = cited_auc if use_cited else activity_auc
        results.append(
            UsageResult(
                config=cfg,
                mean=statistics.fmean(scores) if scores else 0.0,
                std=statistics.pstdev(scores) if len(scores) > 1 else 0.0,
                frac_zero=_fraction(scores, lambda v: v <= 1e-9),
                frac_saturated=_fraction(scores, lambda v: v >= 0.999),
                cited_auc=cited_auc,
                activity_auc=activity_auc,
                ranking_auc=ranking_auc,
                ranked_by="cited" if use_cited else "activity",
            )
        )
    # Highest separation first; a lower saturated fraction breaks ties (avoid runaway).
    results.sort(key=lambda r: (r.ranking_auc, -r.frac_saturated), reverse=True)
    return results


def usage_report(results: Sequence[UsageResult], top: int) -> list[str]:
    if not results:
        return ["Usage signal — no configs evaluated."]
    ranked_by = results[0].ranked_by
    out = [
        f"Usage signal — top {min(top, len(results))} configs ranked by {ranked_by}-AUC "
        "(higher = better separation of used vs cold nodes):",
        f"  {'config':<28}  {'mean':>5}  {'std':>5}  {'zero%':>6}  "
        f"{'sat%':>5}  {'citedAUC':>8}  {'actAUC':>7}",
    ]
    for r in results[:top]:
        out.append(
            f"  {r.config.label():<28}  {r.mean:>5.3f}  {r.std:>5.3f}  "
            f"{r.frac_zero * 100:>5.1f}%  {r.frac_saturated * 100:>4.1f}%  "
            f"{r.cited_auc:>8.3f}  {r.activity_auc:>7.3f}"
        )
    if ranked_by == "activity":
        out.append(
            "  NOTE: too few cited nodes for citation-AUC — ranked by retrieval-activity "
            "AUC instead (revisit once fc4b0669 lands explicit feedback)."
        )
    return out


def build_configs() -> list[UsageConfig]:
    configs: list[UsageConfig] = []
    for fw, rw in _FREQ_RECENCY_SPLITS:
        for hl in _HALFLIVES:
            for k in _NORM_KS:
                configs.append(UsageConfig(fw, rw, hl, k))
    return configs


def _parse_floors(raw: str) -> list[float]:
    return [float(x) for x in raw.split(",") if x.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stats_db", help="Path to a COPY of stats.db (read-only)")
    parser.add_argument(
        "--floors",
        type=_parse_floors,
        default=list(_DEFAULT_FLOORS),
        help="Comma-separated candidate floors (default: 0.2,0.25,0.3,0.35,0.4)",
    )
    parser.add_argument("--top", type=int, default=10, help="How many usage configs to print")
    args = parser.parse_args(argv)

    nodes = load_nodes(args.stats_db)
    if not nodes:
        print("No node_stats rows found.", file=sys.stderr)
        return 1

    cited = sum(1 for n in nodes if n.cited_count > 0)
    print(f"Loaded {len(nodes)} nodes ({cited} with citations).\n")
    for line in floor_report(nodes, args.floors):
        print(line)
    print()
    results = evaluate_usage(nodes, build_configs())
    for line in usage_report(results, args.top):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
