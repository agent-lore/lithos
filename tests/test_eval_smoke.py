"""E7 smoke gate: parser + scorer over the committed fixtures, no network.

Runs inside ``make test``; ``make eval-smoke`` runs only this file. A parser
or scorer regression shows up here as a changed verdict on a recorded
completion, so the assertions pin what the harness must see in each
recorded set, not just that it runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lithos.config import LlmConfig
from lithos.lcma.edge_inference import build_adjudication_prompt
from lithos.lcma.eval.cases import check_labels_cover_cases, load_cases, load_labels
from lithos.lcma.eval.compare import gate, load_policy
from lithos.lcma.eval.prices import load_prices
from lithos.lcma.eval.runner import build_run_document, results_from_recorded
from lithos.lcma.eval.scoring import compute_metrics

EDGES = Path("evals/edges/seed")
PRODUCTION = Path("evals/edges/cases.jsonl")
CASES = load_cases(EDGES / "cases.jsonl")
LABELS = load_labels(EDGES / "labels.jsonl")


def _recorded(name: str) -> dict[str, str]:
    rows = [
        json.loads(line)
        for line in (EDGES / "recorded" / name).read_text().splitlines()
        if line.strip()
    ]
    return {row["case_id"]: row["completion"] for row in rows}


def _metrics(name: str):
    results = results_from_recorded(CASES, _recorded(name))
    assert len(results) == len(CASES), f"{name} must cover every case"
    return compute_metrics(CASES, results, labels=LABELS, confidence_floor=0.6)


def test_fixtures_are_consistent() -> None:
    assert check_labels_cover_cases(LABELS, CASES) == []
    assert len(CASES) >= 6
    relations = {label.relation for label in LABELS.values()}
    assert relations >= {
        "supports",
        "contradicts",
        "refines",
        "is_example_of",
        "depends_on",
        "analogy_to",
        "none",
    }
    assert any("negative:" in tag for case in CASES for tag in case.tags)
    assert load_prices(Path("evals/prices.toml"))
    assert load_policy(Path("evals/POLICY.md"))


def test_incumbent_recording_scores_as_expected() -> None:
    m = _metrics("seed-incumbent.jsonl")
    assert m["rates"]["ok"] == 1.0
    assert m["contract_violation_rate"] == 0.0
    # one deliberate miss out of the labelled slots (seed-004 candidate 1)
    assert m["labels"]["relation_agreement"] == pytest.approx((len(LABELS) - 1) / len(LABELS))
    # the missed depends_on slot is directed, so it is a direction miss too (4 directed truth slots)
    assert m["labels"]["n_directed"] == 4
    assert m["labels"]["direction_agreement"] == pytest.approx(3 / 4)
    assert m["labels"]["by_relation"]["depends_on"]["relation_agreement"] == 0.0
    assert m["labels"]["calibration"]["<0.6"]["n"] == 1  # the miss was the low-confidence answer
    assert m["reference"]["relation_agreement"] == 1.0  # it *is* the reference


def test_constant_direction_failure_is_visible() -> None:
    m = _metrics("seed-constant-direction.jsonl")
    assert m["rates"]["ok"] == 1.0  # perfect mechanics...
    assert m["labels"]["relation_agreement"] == 1.0
    assert m["labels"]["direction_agreement"] == 0.0  # ...every direction wrong
    verdict = gate(_metrics("seed-incumbent.jsonl"), m)
    assert not verdict.promotable
    assert next(c.name for c in verdict.checks if not c.passed) == "direction_agreement"


def test_malformed_completion_is_a_parse_error_not_a_crash() -> None:
    m = _metrics("seed-malformed.jsonl")
    assert m["outcomes"]["parse_error"] == 1
    assert m["rates"]["parse_error"] == pytest.approx(1 / len(CASES))
    # the unusable case's labelled slots count as misses
    assert m["labels"]["relation_agreement"] < 1.0


def test_run_document_builds_from_recordings() -> None:
    cfg = LlmConfig(base_url="http://x/v1", model="seed-incumbent")
    doc = build_run_document(
        artefact="edges",
        cases=CASES,
        results=results_from_recorded(CASES, _recorded("seed-incumbent.jsonl")),
        llm_config=cfg,
        labels=LABELS,
        price=None,
        cases_path=EDGES / "cases.jsonl",
        labels_path=EDGES / "labels.jsonl",
        recorded=True,
    )
    assert doc["recorded"] is True
    assert doc["metrics"]["cost_per_1k_calls"] is None  # seed model has no price row


def test_production_set_replays_its_reference() -> None:
    """The committed production set loads, renders and re-scores its own reference."""
    cases = load_cases(PRODUCTION)
    assert len({case.case_id for case in cases}) == len(cases)
    exported = [case for case in cases if case.reference is not None]
    for case in exported:
        # Hand-built cases can only be rebuilt; this pins that a rebuild is
        # what the incumbent saw for the exported ones.
        rebuilt = build_adjudication_prompt(
            str(case.focus["title"]),
            str(case.focus["content"]),
            case.neighbours(),
            snippet_chars=case.snippet_chars,
        )
        assert case.messages == [{"role": m.role, "content": m.content} for m in rebuilt]
    assert all(
        any(tag.startswith(("negative:", "lang:")) for tag in case.tags)
        for case in cases
        if case.reference is None
    )
    recorded = {case.case_id: str(case.reference["completion"]) for case in exported}
    m = compute_metrics(
        exported, results_from_recorded(exported, recorded), labels={}, confidence_floor=0.6
    )
    assert m["outcomes"].get("parse_error", 0) == 0
    assert m["reference"]["relation_agreement"] == 1.0
