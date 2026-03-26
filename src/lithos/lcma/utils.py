"""LCMA utility helpers.

Standalone functions used by the LCMA pipeline.  Kept separate from the
scout/retrieval machinery so they can be tested and imported independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Candidate:
    """Internal working type during the PTS pipeline.

    Holds a node reference with a raw (pre-normalisation) or normalised score,
    together with the provenance information that explains *why* this node was
    surfaced (free-text reasons) and *which* scouts produced it.

    After ``merge_and_normalize`` the score is always in ``[0, 1]``.
    """

    node_id: str
    score: float
    reasons: list[str] = field(default_factory=list)
    scouts: list[str] = field(default_factory=list)


def merge_and_normalize(candidates: list[Candidate]) -> list[Candidate]:
    """Merge candidates by node_id and normalise scores to ``[0, 1]``.

    Implements per-scout min-max normalisation followed by a merge step
    that deduplicates by ``node_id`` (see design doc §5.3.1).

    **Normalisation** is applied per scout:

    * Within each scout's candidate group, the highest raw score maps to
      ``1.0`` and the lowest to ``0.0``.
    * If every candidate in a scout group has the *same* raw score the span
      is zero — all candidates in that group receive a normalised score of
      ``1.0`` (i.e. the single-element case is treated as a perfect match).

    **Merge** deduplicates across scouts by ``node_id``:

    * The merged candidate keeps the *maximum* normalised score.
    * ``reasons`` lists are concatenated (order of first encounter preserved).
    * ``scouts`` lists are unioned (duplicates removed, order not guaranteed).

    The input list is **not mutated**; new ``Candidate`` objects are returned.

    Args:
        candidates: Raw candidates from one or more scouts.  May be empty.

    Returns:
        Deduplicated, normalised candidates in an unspecified order.
        The caller is responsible for any final ranking/sorting.
    """
    if not candidates:
        return []

    # Guard: each input Candidate must come from exactly one scout.
    # Multi-scout Candidates are produced as *output* of this function, not
    # accepted as input (they would corrupt the per-scout normalisation step).
    for c in candidates:
        if len(c.scouts) > 1:
            raise ValueError(
                f"merge_and_normalize expects single-scout Candidates as input; "
                f"got scouts={c.scouts!r} for node_id={c.node_id!r}. "
                "Multi-scout Candidates are produced as output, not accepted as input."
            )

    # --- Step 1: group by scout name ---
    by_scout: dict[str, list[Candidate]] = {}
    for c in candidates:
        # A candidate may list multiple scouts; use the first scout as the
        # grouping key (each scout is expected to emit its own Candidate
        # objects with a single entry in scouts).
        key = c.scouts[0] if c.scouts else "__unknown__"
        by_scout.setdefault(key, []).append(c)

    # --- Step 2: per-scout min-max normalisation (non-mutating) ---
    normalised: list[Candidate] = []
    for group in by_scout.values():
        scores = [c.score for c in group]
        lo = min(scores)
        hi = max(scores)
        uniform = hi <= lo  # single element or all equal
        span = (hi - lo) if not uniform else 1.0
        for c in group:
            # When all scores are equal (including a single-element group)
            # every candidate is a perfect match → normalise to 1.0.
            norm_score = 1.0 if uniform else (c.score - lo) / span
            normalised.append(
                Candidate(
                    node_id=c.node_id,
                    score=norm_score,
                    reasons=list(c.reasons),
                    scouts=list(c.scouts),
                )
            )

    # --- Step 3: merge by node_id (max score, union reasons+scouts) ---
    merged: dict[str, Candidate] = {}
    for c in normalised:
        if c.node_id in merged:
            existing = merged[c.node_id]
            if c.score > existing.score:
                existing.score = c.score
            existing.reasons.extend(c.reasons)
            existing.scouts = list(set(existing.scouts) | set(c.scouts))
        else:
            merged[c.node_id] = Candidate(
                node_id=c.node_id,
                score=c.score,
                reasons=list(c.reasons),
                scouts=list(c.scouts),
            )

    return list(merged.values())
