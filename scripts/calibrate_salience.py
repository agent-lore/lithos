#!/usr/bin/env python3
"""Offline calibration harness for LCMA salience recalibration (task e7d8ef60).

Runs **read-only against a copy of a prod/staging ``stats.db``** — never the live
instance — and helps pick the shipped defaults for the salience floor and the usage
signal. It imports the same pure helpers the server uses
(:mod:`lithos.lcma.salience`), so the numbers reflect exactly what runs in production.

It answers two questions:

1. **Floor** — for each candidate floor, what does the salience distribution look like
   after the one-time backfill? The projection uses the *same* eligibility predicate as
   the operator run (:func:`lithos.lcma.salience.recalibration_eligible`), so
   misleading / chronically-ignored nodes are excluded exactly as production excludes
   them.
2. **Usage signal** — for each candidate ``usage_score`` parameter set, how well does it
   *separate* nodes that were genuinely used from cold ones? Separation is scored with a
   rank-based AUC against an **independent** label:

   - **citation-AUC** — cited (``cited_count > 0``) vs not; independent of retrieval.
   - **future-retrieval-AUC** — a time split of the ``receipts`` log: score each node
     from its *past* retrievals only, label it by whether it is retrieved again in the
     held-out *future* window. This is non-circular (the label is not the score's own
     input) and is what actually calibrates frequency vs recency vs half-life.

   When neither label has enough support (the sparse-feedback regime), the harness
   reports distribution diagnostics only and does **not** claim a best config.

Usage:
    python scripts/calibrate_salience.py /path/to/stats-copy.db
    python scripts/calibrate_salience.py stats.db --floors 0.25,0.3,0.35 --split 0.7 --top 8
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from lithos.lcma.salience import recalibration_eligible, usage_score

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
_MIN_LABEL_SUPPORT = 10  # need this many positives AND negatives to trust an AUC


@dataclass(frozen=True)
class NodeRow:
    """The node_stats fields the calibration needs."""

    node_id: str
    salience: float
    retrieval_count: int
    cited_count: int
    misleading_count: int
    ignored_count: int
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

    def score(self, retrieval_count: int, days_since_use: float | None) -> float:
        return usage_score(
            retrieval_count,
            days_since_use,
            freq_weight=self.freq_weight,
            recency_weight=self.recency_weight,
            recency_halflife_days=self.recency_halflife_days,
            freq_norm_k=self.freq_norm_k,
        )


@dataclass(frozen=True)
class TimeSplit:
    """A past/future partition of the receipts log for held-out evaluation."""

    split_time: datetime
    past_count: dict[str, int]
    past_last: dict[str, datetime]
    future_nodes: frozenset[str]


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
            "SELECT node_id, salience, retrieval_count, cited_count, misleading_count, "
            "ignored_count, last_used_at, last_retrieved_at FROM node_stats"
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
                node_id=str(row["node_id"]),
                salience=float(row["salience"]),
                retrieval_count=int(row["retrieval_count"]),
                cited_count=int(row["cited_count"]),
                misleading_count=int(row["misleading_count"]),
                ignored_count=int(row["ignored_count"]),
                days_since_use=days,
            )
        )
    return nodes


def load_receipts(db_path: str) -> list[tuple[datetime, list[str]]]:
    """Read (ts, returned node_ids) from the receipts log of a stats.db copy."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT ts, final_nodes FROM receipts").fetchall()
    finally:
        conn.close()

    out: list[tuple[datetime, list[str]]] = []
    for ts_raw, nodes_raw in rows:
        ts = _parse_ts(ts_raw)
        if ts is None:
            continue
        try:
            node_ids = json.loads(nodes_raw) if nodes_raw else []
        except (TypeError, ValueError):
            continue
        if isinstance(node_ids, list):
            out.append((ts, [str(n) for n in node_ids]))
    return out


def build_time_split(
    receipts: Sequence[tuple[datetime, list[str]]], split_quantile: float
) -> TimeSplit | None:
    """Partition receipts by time: earlier fraction = past (scored), rest = future (label)."""
    if not receipts:
        return None
    ordered = sorted(receipts, key=lambda r: r[0])
    idx = min(len(ordered) - 1, max(0, int(split_quantile * len(ordered))))
    split_time = ordered[idx][0]
    past_count: dict[str, int] = {}
    past_last: dict[str, datetime] = {}
    future_nodes: set[str] = set()
    for ts, node_ids in ordered:
        if ts <= split_time:
            for nid in node_ids:
                past_count[nid] = past_count.get(nid, 0) + 1
                prev = past_last.get(nid)
                if prev is None or ts > prev:
                    past_last[nid] = ts
        else:
            future_nodes.update(node_ids)
    return TimeSplit(split_time, past_count, past_last, frozenset(future_nodes))


def auc(positives: Sequence[float], negatives: Sequence[float]) -> float:
    """Probability a random positive outranks a random negative (ties count 0.5).

    Returns 0.5 (uninformative) when either group is empty. O(n log n) via rank sums
    (Mann-Whitney U), so it scales to the whole corpus.
    """
    if not positives or not negatives:
        return 0.5
    labelled = [(v, 1) for v in positives] + [(v, 0) for v in negatives]
    labelled.sort(key=lambda t: t[0])
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
    idx = min(len(sorted_values), max(1, int(-(-q * len(sorted_values)) // 1))) - 1
    return sorted_values[idx]


def floor_report(nodes: Sequence[NodeRow], floors: Sequence[float]) -> list[str]:
    """One row per candidate floor: the salience distribution after the *real* backfill.

    Projection uses :func:`recalibration_eligible`, so it excludes misleading /
    chronically-ignored nodes exactly as the operator ``recalibrate-salience`` run does.
    """
    out = ["Floor backfill projection (matches the operator eligibility predicate):"]
    out.append(
        f"  {'floor':>6}  {'lift%':>6}  {'mean':>6}  {'<floor':>7}  "
        f"{'std':>6}  {'p50':>6}  {'p90':>6}"
    )
    for floor in floors:
        projected: list[float] = []
        lifted = 0
        for n in nodes:
            if recalibration_eligible(
                n.salience,
                floor,
                misleading_count=n.misleading_count,
                ignored_count=n.ignored_count,
                cited_count=n.cited_count,
            ):
                projected.append(floor)
                lifted += 1
            else:
                projected.append(n.salience)
        s = sorted(projected)
        mean = statistics.fmean(projected) if projected else 0.0
        std = statistics.pstdev(projected) if len(projected) > 1 else 0.0
        below = (sum(1 for v in projected if v < floor) / len(projected)) if projected else 0.0
        lift_pct = (lifted / len(nodes) * 100) if nodes else 0.0
        out.append(
            f"  {floor:>6.2f}  {lift_pct:>5.1f}%  {mean:>6.3f}  {below * 100:>6.1f}%  "
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
    cited_auc: float | None
    future_auc: float | None
    ranking_auc: float | None
    ranked_by: str


def _current_scores(nodes: Sequence[NodeRow], cfg: UsageConfig) -> list[float]:
    return [cfg.score(n.retrieval_count, n.days_since_use) for n in nodes]


def _cited_auc(nodes: Sequence[NodeRow], scores: Sequence[float]) -> tuple[float | None, bool]:
    pos = [s for s, n in zip(scores, nodes, strict=True) if n.cited_count > 0]
    neg = [s for s, n in zip(scores, nodes, strict=True) if n.cited_count == 0]
    ok = len(pos) >= _MIN_LABEL_SUPPORT and len(neg) >= _MIN_LABEL_SUPPORT
    return (auc(pos, neg) if (pos and neg) else None), ok


def _future_auc(split: TimeSplit | None, cfg: UsageConfig) -> tuple[float | None, bool]:
    """Score each past-seen node from its PAST usage; label = retrieved in the future."""
    if split is None or not split.past_count:
        return None, False
    pos: list[float] = []
    neg: list[float] = []
    for nid, count in split.past_count.items():
        last = split.past_last.get(nid)
        days = None if last is None else max(0.0, (split.split_time - last).total_seconds() / 86400)
        s = cfg.score(count, days)
        (pos if nid in split.future_nodes else neg).append(s)
    ok = len(pos) >= _MIN_LABEL_SUPPORT and len(neg) >= _MIN_LABEL_SUPPORT
    return (auc(pos, neg) if (pos and neg) else None), ok


def evaluate_usage(
    nodes: Sequence[NodeRow],
    configs: Sequence[UsageConfig],
    split: TimeSplit | None = None,
) -> list[UsageResult]:
    """Score every node under each config and rank configs by an *independent* signal.

    Ranking uses citation-AUC when citations are dense enough, else the non-circular
    future-retrieval-AUC from the receipts time split, else neither — in which case
    ``ranked_by == "none"`` and the ordering is descriptive (by spread), not a
    recommendation.
    """
    # Decide the ranking label once, from the first config's support (support is a
    # property of the data + label, not the config).
    probe = configs[0] if configs else UsageConfig(0.6, 0.4, 14.0, 20.0)
    _, cited_ok = _cited_auc(nodes, _current_scores(nodes, probe))
    _, future_ok = _future_auc(split, probe)
    ranked_by = "cited" if cited_ok else "future" if future_ok else "none"

    results: list[UsageResult] = []
    for cfg in configs:
        scores = _current_scores(nodes, cfg)
        cited_auc, _ = _cited_auc(nodes, scores)
        future_auc, _ = _future_auc(split, cfg)
        ranking = (
            cited_auc if ranked_by == "cited" else future_auc if ranked_by == "future" else None
        )
        results.append(
            UsageResult(
                config=cfg,
                mean=statistics.fmean(scores) if scores else 0.0,
                std=statistics.pstdev(scores) if len(scores) > 1 else 0.0,
                frac_zero=_fraction(scores, lambda v: v <= 1e-9),
                frac_saturated=_fraction(scores, lambda v: v >= 0.999),
                cited_auc=cited_auc,
                future_auc=future_auc,
                ranking_auc=ranking,
                ranked_by=ranked_by,
            )
        )
    # When we have an independent signal, rank by it (a lower saturated fraction breaks
    # ties, to avoid runaway). Otherwise fall back to descriptive spread, largest first.
    if ranked_by == "none":
        results.sort(key=lambda r: (r.std, -r.frac_saturated), reverse=True)
    else:
        results.sort(key=lambda r: ((r.ranking_auc or 0.0), -r.frac_saturated), reverse=True)
    return results


def _fmt_auc(v: float | None) -> str:
    return f"{v:.3f}" if v is not None else "  n/a"


def usage_report(results: Sequence[UsageResult], top: int) -> list[str]:
    if not results:
        return ["Usage signal — no configs evaluated."]
    ranked_by = results[0].ranked_by
    header = {
        "cited": "ranked by citation-AUC (independent of retrieval)",
        "future": "ranked by future-retrieval-AUC (held-out receipts time split)",
        "none": "NO independent label with enough support — ordering is descriptive "
        "(by spread) only, NOT a recommendation; set defaults by judgment or gather "
        "feedback (fc4b0669)",
    }[ranked_by]
    out = [
        f"Usage signal — top {min(top, len(results))} of {len(results)} configs; {header}:",
        f"  {'config':<28}  {'mean':>5}  {'std':>5}  {'zero%':>6}  "
        f"{'sat%':>5}  {'citedAUC':>8}  {'futAUC':>7}",
    ]
    for r in results[:top]:
        out.append(
            f"  {r.config.label():<28}  {r.mean:>5.3f}  {r.std:>5.3f}  "
            f"{r.frac_zero * 100:>5.1f}%  {r.frac_saturated * 100:>4.1f}%  "
            f"{_fmt_auc(r.cited_auc):>8}  {_fmt_auc(r.future_auc):>7}"
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
    parser.add_argument(
        "--split",
        type=float,
        default=0.7,
        help="Past/future receipts split quantile for future-retrieval-AUC (default: 0.7)",
    )
    parser.add_argument("--top", type=int, default=10, help="How many usage configs to print")
    args = parser.parse_args(argv)

    nodes = load_nodes(args.stats_db)
    if not nodes:
        print("No node_stats rows found.", file=sys.stderr)
        return 1

    receipts = load_receipts(args.stats_db)
    split = build_time_split(receipts, args.split)
    cited = sum(1 for n in nodes if n.cited_count > 0)
    print(
        f"Loaded {len(nodes)} nodes ({cited} cited), {len(receipts)} receipts"
        + (f", time split at {split.split_time.isoformat()}." if split else ".")
        + "\n"
    )
    for line in floor_report(nodes, args.floors):
        print(line)
    print()
    for line in usage_report(evaluate_usage(nodes, build_configs(), split), args.top):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
