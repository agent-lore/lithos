"""Ranking metrics over a labelled pool, per method and per slice.

Pooled evaluation: a pair without a label counts as not relevant, and the
share of such slots in each method's top-k is reported so a thin pool is
visible rather than silently flattering. nDCG is graded (relevant 2,
related 1, not 0); precision and reciprocal rank count ``relevant``
only. A query with no relevant note in its pool has no ideal ranking, so it
is left out of the nDCG and MRR means and counted separately.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from lithos.lcma.eval.retrieval.cases import (
    METHODS,
    RELEVANT,
    RetrievalCase,
    RetrievalLabel,
    Slot,
)

Grades = Mapping[str, int]  # note_id -> gain, for one case


def dcg(gains: Sequence[int]) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def ndcg_at_k(ranked_ids: Sequence[str], grades: Grades, k: int) -> float | None:
    """Graded nDCG@k; ``None`` when the case has no note with a positive gain."""
    ideal = sorted((g for g in grades.values() if g > 0), reverse=True)[:k]
    if not ideal:
        return None
    got = [grades.get(note_id, 0) for note_id in ranked_ids[:k]]
    return dcg(got) / dcg(ideal)


def precision_at_k(ranked_ids: Sequence[str], grades: Grades, k: int) -> float:
    top = ranked_ids[:k]
    if not top:
        return 0.0
    return sum(1 for note_id in top if grades.get(note_id, 0) >= RELEVANT) / k


def reciprocal_rank(ranked_ids: Sequence[str], grades: Grades) -> float | None:
    """1/rank of the first ``relevant`` note; ``None`` when the case has none in its pool."""
    if not any(g >= RELEVANT for g in grades.values()):
        return None
    for i, note_id in enumerate(ranked_ids, start=1):
        if grades.get(note_id, 0) >= RELEVANT:
            return 1.0 / i
    return 0.0


def mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def grades_for(case: RetrievalCase, labels: Mapping[Slot, RetrievalLabel]) -> dict[str, int]:
    return {
        note_id: labels[(case.case_id, note_id)].gain
        for note_id in case.pooled_ids()
        if (case.case_id, note_id) in labels
    }


@dataclass(frozen=True)
class MethodMetrics:
    method: str
    n_queries: int
    n_with_relevant: int
    ndcg_at_k: float | None
    precision_at_3: float | None
    precision_at_5: float | None
    mrr: float | None
    unlabelled_share: float | None  # share of top-k slots without a label
    latency_ms_median: float | None
    latency_ms_p90: float | None


def score_method(
    cases: Sequence[RetrievalCase],
    labels: Mapping[Slot, RetrievalLabel],
    method: str,
    *,
    k: int,
) -> MethodMetrics:
    ndcgs: list[float] = []
    rrs: list[float] = []
    p3s: list[float] = []
    p5s: list[float] = []
    latencies: list[float] = []
    slots = 0
    unlabelled = 0
    n_with_relevant = 0
    for case in cases:
        run = case.methods.get(method)
        if run is None:
            continue
        grades = grades_for(case, labels)
        ids = run.ids
        latencies.append(run.latency_ms)
        p3s.append(precision_at_k(ids, grades, 3))
        p5s.append(precision_at_k(ids, grades, 5))
        top = ids[:k]
        slots += len(top)
        unlabelled += sum(1 for note_id in top if note_id not in grades)
        ndcg = ndcg_at_k(ids, grades, k)
        if ndcg is not None:
            ndcgs.append(ndcg)
        rr = reciprocal_rank(ids, grades)
        if rr is not None:
            rrs.append(rr)
            n_with_relevant += 1
    return MethodMetrics(
        method=method,
        n_queries=len(latencies),
        n_with_relevant=n_with_relevant,
        ndcg_at_k=mean(ndcgs),
        precision_at_3=mean(p3s),
        precision_at_5=mean(p5s),
        mrr=mean(rrs),
        unlabelled_share=(unlabelled / slots) if slots else None,
        latency_ms_median=percentile(latencies, 0.5),
        latency_ms_p90=percentile(latencies, 0.9),
    )


def score_cases(
    cases: Sequence[RetrievalCase],
    labels: Mapping[Slot, RetrievalLabel],
    *,
    k: int = 10,
) -> dict[str, Any]:
    """Per-method metrics overall and per slice, as plain dicts for the run document."""
    methods = [m for m in METHODS if any(m in c.methods for c in cases)]
    by_slice: dict[str, dict[str, Any]] = {}
    for slice_name in sorted({c.slice for c in cases}):
        subset = [c for c in cases if c.slice == slice_name]
        by_slice[slice_name] = {m: asdict(score_method(subset, labels, m, k=k)) for m in methods}
    return {
        "k": k,
        "n_cases": len(cases),
        "n_labels": len(labels),
        "overall": {m: asdict(score_method(cases, labels, m, k=k)) for m in methods},
        "by_slice": by_slice,
    }


# ── Relevance floor ────────────────────────────────────────────────────────

Signal = Callable[[RetrievalCase], tuple[str, float] | None]
"""The top result of one method and the score a caller could floor on."""


def _top(case: RetrievalCase, method: str, attr: str) -> tuple[str, float] | None:
    run = case.methods.get(method)
    if run is None or not run.results:
        return None
    first = run.results[0]
    value = getattr(first, attr)
    return None if value is None else (first.id, float(value))


SIGNALS: dict[str, Signal] = {
    "semantic": lambda c: _top(c, "semantic", "score"),
    "retrieve": lambda c: _top(c, "retrieve", "score"),
    "retrieve_vector": lambda c: _top(c, "retrieve", "vector_similarity"),
}

THRESHOLDS: tuple[float, ...] = tuple(round(i * 0.05, 2) for i in range(0, 21))


def _split_by_relevance(
    cases: Sequence[RetrievalCase], labels: Mapping[Slot, RetrievalLabel]
) -> tuple[list[RetrievalCase], list[RetrievalCase]]:
    """Labelled cases with a ``relevant`` note in the pool, and labelled cases without one."""
    with_relevant: list[RetrievalCase] = []
    without_relevant: list[RetrievalCase] = []
    for case in cases:
        grades = grades_for(case, labels)
        if not grades:
            continue
        if any(g >= RELEVANT for g in grades.values()):
            with_relevant.append(case)
        else:
            without_relevant.append(case)
    return with_relevant, without_relevant


def _supplied(signal: Signal, case: RetrievalCase, threshold: float) -> tuple[str, float] | None:
    top = signal(case)
    return top if top is not None and top[1] >= threshold else None


def _rates(
    signal: Signal,
    threshold: float,
    *,
    with_relevant: Sequence[RetrievalCase],
    without_relevant: Sequence[RetrievalCase],
    labels: Mapping[Slot, RetrievalLabel],
) -> tuple[float | None, float | None]:
    hits = sum(
        1
        for case in with_relevant
        if (top := _supplied(signal, case, threshold)) is not None
        and (label := labels.get((case.case_id, top[0]))) is not None
        and label.gain >= RELEVANT
    )
    false = sum(1 for case in without_relevant if _supplied(signal, case, threshold) is not None)
    return (
        hits / len(with_relevant) if with_relevant else None,
        false / len(without_relevant) if without_relevant else None,
    )


def floor_sweep(
    cases: Sequence[RetrievalCase],
    labels: Mapping[Slot, RetrievalLabel],
    *,
    floor: float,
    thresholds: Sequence[float] = THRESHOLDS,
) -> dict[str, Any]:
    """How each score signal behaves as a 'supply only if relevant' floor.

    For every threshold: among queries that have a ``relevant`` note in the
    pool, the share where the method's top result clears the floor *and* is
    relevant (``hit_rate``); among queries whose labelled pool holds nothing
    relevant, the share where something clears the floor anyway
    (``false_supply_rate``). ``at_floor`` reads both at the caller's floor.

    As in the ranking metrics, a top result without a label counts as not
    relevant (a miss, or a false supply); ``unlabelled_share`` in the method
    metrics says how much of that there was.
    """
    with_relevant, without_relevant = _split_by_relevance(cases, labels)
    groups = {
        "with_relevant": with_relevant,
        "without_relevant": without_relevant,
        "labels": labels,
    }
    signals: dict[str, Any] = {}
    for name, signal in SIGNALS.items():
        if not any(signal(c) is not None for c in cases):
            continue
        swept = [_rates(signal, t, **groups) for t in thresholds]
        hit, false = _rates(signal, floor, **groups)
        signals[name] = {
            "thresholds": list(thresholds),
            "hit_rate": [h for h, _ in swept],
            "false_supply_rate": [f for _, f in swept],
            "at_floor": {"floor": floor, "hit_rate": hit, "false_supply_rate": false},
        }
    return {
        "n_with_relevant": len(with_relevant),
        "n_without_relevant": len(without_relevant),
        "signals": signals,
    }
