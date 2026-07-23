"""Pure salience math: decay toward a non-zero floor + a non-decaying usage signal.

This module is the single source of truth for how salience erodes over time and how
a node's live usage counters translate into a rerank signal. Both the live worker
(:mod:`lithos.lcma.enrich`) / reranker (:mod:`lithos.lcma.retrieve`) **and** the offline
calibration harness import these functions, so calibration reflects exactly what runs in
production.

Everything here is a pure function — plain numbers in, a number out, no I/O and no config
object — so the helpers stay trivially unit-testable and parameter-sweepable. Callers pull
the tunable constants from :class:`lithos.config.LcmaConfig` and pass them explicitly.

Background (task ``e7d8ef60``): the original model decayed untouched nodes linearly to
*zero* with no floor, while the only forces that raised salience fired solely on actively
used nodes. On a large, mostly-cold corpus that drove ~86% of nodes to ~0, turning the
rerank salience term into a near-uniform penalty. The floor stops the collapse; the usage
signal restores a real spread from counters that cannot collapse.
"""

from __future__ import annotations

import math

#: Neutral salience assigned to a node on first touch and used as the rerank fallback
#: when a node has no stats row yet.
DEFAULT_SALIENCE = 0.5


def decay_amount(
    current_salience: float,
    days_inactive: int,
    *,
    per_day: float,
    daily_cap: float,
    floor: float,
) -> float:
    """Return the salience decay to subtract for a node idle *days_inactive* days.

    Linear in idle days (``per_day`` per day), capped at ``daily_cap`` per application,
    and never enough to push salience below ``floor`` — time decay erodes toward a
    non-zero resting baseline, not to zero. The returned amount is always non-negative
    (``0.0`` once the node is already at or below the floor).

    The floor applies to *time decay only*. Explicit negative feedback
    (misleading/ignored) is a separate path that may still drive salience below the
    floor, because those are deliberate quality signals rather than mere inactivity.
    """
    raw = min(daily_cap, max(0, days_inactive) * per_day)
    headroom = max(0.0, current_salience - floor)
    return min(raw, headroom)


def recalibration_eligible(
    salience: float,
    floor: float,
    *,
    misleading_count: int,
    ignored_count: int,
    cited_count: int,
) -> bool:
    """Whether the one-time floor backfill should lift *salience* to *floor*.

    True iff the node is **strictly below** the floor and carries no explicit
    negative-feedback signal: a node penalised as misleading, or chronically ignored
    (ignored more than five times and more than it was cited), keeps its deliberate
    sub-floor value. This is the single source of truth for backfill eligibility —
    mirrored by the SQL ``WHERE`` clause in
    :meth:`lithos.lcma.stats.StatsStore.recalibrate_salience_floor` and reused by the
    offline calibration harness so its projected distribution matches the operator run.
    """
    chronically_ignored = ignored_count > 5 and ignored_count > cited_count
    return not (salience >= floor or misleading_count > 0 or chronically_ignored)


def usage_score(
    retrieval_count: int,
    days_since_use: float | None,
    *,
    freq_weight: float,
    recency_weight: float,
    recency_halflife_days: float,
    freq_norm_k: float,
) -> float:
    """Return a bounded ``[0, 1]`` popularity signal from live usage counters.

    Combines log-scaled retrieval frequency (diminishing returns, normalised so
    ``freq_norm_k`` retrievals map to ~1.0) with exponential recency (value halves every
    ``recency_halflife_days``). The result is monotonic increasing in ``retrieval_count``
    and decreasing in ``days_since_use``, and is clamped to ``[0, 1]``.

    Unlike stored salience this cannot collapse: ``retrieval_count`` is monotonic and the
    recency term is recomputed live at read time, so the signal is always meaningful even
    on a cold corpus with no reinforcement. ``days_since_use`` of ``None`` (a node never
    used) contributes no recency.
    """
    freq = math.log1p(max(0, retrieval_count)) / math.log1p(max(freq_norm_k, 1.0))
    freq = min(1.0, freq)
    if days_since_use is None:
        recency = 0.0
    else:
        recency = 0.5 ** (max(0.0, days_since_use) / recency_halflife_days)
    score = freq_weight * freq + recency_weight * recency
    return max(0.0, min(1.0, score))
