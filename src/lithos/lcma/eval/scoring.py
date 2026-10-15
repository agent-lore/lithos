"""Scoring a run of the typed-edge artefact (eval suite E3 metrics).

Pure functions over ``(case, completion)``: classify each completion with
the production parser, then aggregate agreement with human labels and with
the reference (incumbent) judgements, confidence calibration, edges that the
engine would write at the confidence floor, token spend, latency and cost.
Every metric is reported together so a cheaper model cannot look good on
cost alone (PRD §E3).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from statistics import median
from typing import Any

from lithos.lcma.edge_inference import SYMMETRIC_RELATIONS, parse_adjudications
from lithos.lcma.eval.cases import Case, Label, Slot
from lithos.lcma.eval.prices import ModelPrice, cost_per_1k_calls

RESULT_OUTCOMES = ("ok", "partial", "empty", "parse_error", "error")
FAILURE_OUTCOMES = ("partial", "empty", "parse_error", "error")

#: Model-confidence buckets for the calibration table; the middle edge is the
#: production confidence floor's default.
CALIBRATION_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("<0.6", 0.0, 0.6),
    ("0.6-0.8", 0.6, 0.8),
    (">=0.8", 0.8, 1.01),
)


@dataclass(frozen=True)
class CaseResult:
    """One completion for one case, as the production parser saw it."""

    case_id: str
    outcome: str
    problems: int
    judgements: list[dict[str, Any]] | None
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int | None
    estimated: bool
    latency_ms: float
    completion: str | None
    error: str | None = None

    def answers(self) -> dict[int, dict[str, Any]]:
        """``{candidate_index: judgement}`` — candidates without one answered ``none``."""
        return {int(j["candidate_index"]): j for j in self.judgements or []}


def classify_completion(
    case: Case, completion: str
) -> tuple[str, int, list[dict[str, Any]] | None]:
    """``(outcome, problems, judgements)`` using the production parser.

    ``empty`` is a well-formed response that judged nothing at all; a
    response that answers ``none`` for every candidate is a clean ``ok``.
    """
    parsed = parse_adjudications(completion, case.n_candidates)
    if parsed is None:
        return "parse_error", case.n_candidates, None
    adjudications, problems = parsed
    judgements = [asdict(a) for a in adjudications]
    if not judgements and problems == case.n_candidates:
        return "empty", problems, judgements
    return ("partial" if problems else "ok"), problems, judgements


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def _ratio(hits: int, total: int) -> float | None:
    return hits / total if total else None


@dataclass
class _Agreement:
    slots: int = 0
    relation_hits: int = 0
    directed: int = 0
    direction_hits: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_slots": self.slots,
            "relation_agreement": _ratio(self.relation_hits, self.slots),
            "n_directed": self.directed,
            "direction_agreement": _ratio(self.direction_hits, self.directed),
        }


def _compare_slot(
    truth_relation: str,
    truth_direction: str | None,
    answer: dict[str, Any] | None,
    *,
    unusable: bool,
    agg: _Agreement,
) -> bool:
    """Score one slot into *agg*; returns whether the relation matched."""
    agg.slots += 1
    if unusable:
        return False
    relation = str(answer["relation"]) if answer is not None else "none"
    matched = relation == truth_relation
    if matched:
        agg.relation_hits += 1
        if truth_relation != "none" and truth_relation not in SYMMETRIC_RELATIONS:
            agg.directed += 1
            if answer is not None and answer.get("direction") == truth_direction:
                agg.direction_hits += 1
    return matched


def score_against_labels(
    cases: list[Case], results: dict[str, CaseResult], labels: dict[Slot, Label]
) -> dict[str, Any]:
    """Agreement with reviewed human labels, by relation and by model confidence."""
    by_id = {c.case_id: c for c in cases}
    overall = _Agreement()
    by_relation: dict[str, _Agreement] = defaultdict(_Agreement)
    calibration: dict[str, list[bool]] = {name: [] for name, _, _ in CALIBRATION_BUCKETS}
    for (case_id, index), label in sorted(labels.items()):
        case = by_id.get(case_id)
        result = results.get(case_id)
        if case is None or result is None or index > case.n_candidates:
            continue
        unusable = result.outcome in ("parse_error", "error")
        answer = None if unusable else result.answers().get(index)
        matched = _compare_slot(
            label.relation, label.direction, answer, unusable=unusable, agg=overall
        )
        _compare_slot(
            label.relation,
            label.direction,
            answer,
            unusable=unusable,
            agg=by_relation[label.relation],
        )
        if answer is not None:
            confidence = float(answer.get("confidence", 0.0))
            for name, low, high in CALIBRATION_BUCKETS:
                if low <= confidence < high:
                    calibration[name].append(matched)
                    break
    return {
        **overall.as_dict(),
        "by_relation": {rel: agg.as_dict() for rel, agg in sorted(by_relation.items())},
        "calibration": {
            name: {"n": len(hits), "agreement": _ratio(sum(hits), len(hits))}
            for name, hits in calibration.items()
        },
    }


def score_against_reference(cases: list[Case], results: dict[str, CaseResult]) -> dict[str, Any]:
    """Agreement with the incumbent's recorded judgements on every candidate slot."""
    agg = _Agreement()
    reference_models: set[str] = set()
    for case in cases:
        result = results.get(case.case_id)
        if result is None or not case.reference:
            continue
        reference_models.add(str(case.reference.get("model", "")))
        truth = case.reference_answers()
        unusable = result.outcome in ("parse_error", "error")
        answers = {} if unusable else result.answers()
        for index in range(1, case.n_candidates + 1):
            relation, direction = truth.get(index, ("none", None))
            _compare_slot(relation, direction, answers.get(index), unusable=unusable, agg=agg)
    return {**agg.as_dict(), "reference_models": sorted(m for m in reference_models if m)}


def compute_metrics(
    cases: list[Case],
    results: list[CaseResult],
    *,
    labels: dict[Slot, Label] | None = None,
    confidence_floor: float,
    price: ModelPrice | None = None,
) -> dict[str, Any]:
    """The E3 table for one run: rates, agreement, calibration, spend, latency, cost."""
    by_id = {r.case_id: r for r in results}
    n = len(results)
    outcomes = {name: sum(1 for r in results if r.outcome == name) for name in RESULT_OUTCOMES}
    parsed = [r for r in results if r.judgements is not None]
    slots = sum(next(c.n_candidates for c in cases if c.case_id == r.case_id) for r in parsed)
    completed = [r for r in results if r.outcome != "error"]
    prompt = [float(r.prompt_tokens) for r in completed]
    completion = [float(r.completion_tokens) for r in completed]
    reasoning_rows = [r for r in completed if r.reasoning_tokens is not None]
    reasoning_total = sum(float(r.reasoning_tokens or 0) for r in reasoning_rows)
    reasoning_denominator = sum(float(r.completion_tokens) for r in reasoning_rows)
    latencies = [r.latency_ms for r in results]
    edges_at_floor = [
        sum(1 for j in (r.judgements or []) if float(j.get("confidence", 0.0)) >= confidence_floor)
        for r in results
    ]
    prompt_mean, completion_mean = mean(prompt), mean(completion)
    return {
        "n_cases": len(cases),
        "n_calls": n,
        "outcomes": outcomes,
        "rates": {name: _ratio(count, n) for name, count in outcomes.items()},
        "contract_violation_rate": _ratio(sum(r.problems for r in parsed), slots),
        "labels": (score_against_labels(cases, by_id, labels) if labels else {"n_slots": 0}),
        "reference": score_against_reference(cases, by_id),
        "confidence_floor": confidence_floor,
        "edges_per_call_at_floor": mean([float(e) for e in edges_at_floor]),
        "tokens": {
            "prompt_mean": prompt_mean,
            "completion_mean": completion_mean,
            "reasoning_mean": (
                mean([float(r.reasoning_tokens or 0) for r in reasoning_rows])
                if reasoning_rows
                else None
            ),
            "reasoning_share": (
                reasoning_total / reasoning_denominator if reasoning_denominator else None
            ),
            "estimated_share": _ratio(sum(1 for r in completed if r.estimated), len(completed)),
        },
        "latency_ms": {
            "p50": median(latencies) if latencies else 0.0,
            "p95": percentile(latencies, 0.95),
        },
        # No completed call means no measured spend: report unknown, not free.
        "cost_per_1k_calls": (
            cost_per_1k_calls(
                price, prompt_tokens_mean=prompt_mean, completion_tokens_mean=completion_mean
            )
            if completed
            else None
        ),
    }
