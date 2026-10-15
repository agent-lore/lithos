"""E3 metrics (lithos.lcma.eval.scoring) and the price table."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lithos.lcma.eval.cases import case_from_dict, label_from_dict
from lithos.lcma.eval.prices import ModelPrice, cost_per_1k_calls, load_prices
from lithos.lcma.eval.scoring import (
    CaseResult,
    classify_completion,
    compute_metrics,
    mean,
    percentile,
)


def _case(case_id="c1", n=2, reference=None):
    return case_from_dict(
        {
            "case_id": case_id,
            "focus": {"id": case_id, "title": "F", "content": "body"},
            "candidates": [
                {
                    "index": i,
                    "node_id": f"{case_id}-{i}",
                    "title": "T",
                    "snippet": "s",
                    "similarity": 0.6,
                    "namespace": "default",
                }
                for i in range(1, n + 1)
            ],
            "reference": reference,
        }
    )


def _judgement(i, relation, direction="focus_to_candidate", confidence=0.9):
    return {
        "candidate": i,
        "relation": relation,
        "direction": direction,
        "confidence": confidence,
        "rationale": "r",
    }


def _completion(*judgements):
    return json.dumps({"judgements": list(judgements)})


def _result(case, completion, **overrides):
    outcome, problems, judgements = classify_completion(case, completion)
    base = {
        "case_id": case.case_id,
        "outcome": outcome,
        "problems": problems,
        "judgements": judgements,
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "reasoning_tokens": None,
        "estimated": False,
        "latency_ms": 500.0,
        "completion": completion,
    }
    base.update(overrides)
    return CaseResult(**base)


class TestClassify:
    def test_outcomes(self) -> None:
        case = _case()
        assert (
            classify_completion(
                case, _completion(_judgement(1, "supports"), _judgement(2, "none"))
            )[0]
            == "ok"
        )
        assert (
            classify_completion(case, _completion(_judgement(1, "none"), _judgement(2, "none")))[0]
            == "ok"
        )
        assert classify_completion(case, _completion(_judgement(1, "supports")))[0] == "partial"
        assert classify_completion(case, _completion())[0] == "empty"
        assert classify_completion(case, "garbage") == ("parse_error", 2, None)


class TestMetrics:
    def test_labels_reference_calibration_and_cost(self) -> None:
        reference = {
            "model": "ref",
            "judgements": [
                {
                    "candidate_index": 1,
                    "relation": "supports",
                    "direction": "focus_to_candidate",
                    "confidence": 0.8,
                    "rationale": "r",
                }
            ],
        }
        c1, c2 = _case("c1", reference=reference), _case("c2", reference=reference)
        labels = {
            ("c1", 1): label_from_dict(
                {
                    "case_id": "c1",
                    "candidate_index": 1,
                    "relation": "supports",
                    "direction": "focus_to_candidate",
                    "labeller": "d",
                    "labelled_at": "x",
                }
            ),
            ("c1", 2): label_from_dict(
                {
                    "case_id": "c1",
                    "candidate_index": 2,
                    "relation": "none",
                    "labeller": "d",
                    "labelled_at": "x",
                }
            ),
            ("c2", 1): label_from_dict(
                {
                    "case_id": "c2",
                    "candidate_index": 1,
                    "relation": "supports",
                    "direction": "candidate_to_focus",
                    "labeller": "d",
                    "labelled_at": "x",
                }
            ),
            ("c2", 2): label_from_dict(
                {
                    "case_id": "c2",
                    "candidate_index": 2,
                    "relation": "contradicts",
                    "labeller": "d",
                    "labelled_at": "x",
                }
            ),
        }
        results = [
            # c1: both slots right (direction right), high confidence
            _result(
                c1, _completion(_judgement(1, "supports", confidence=0.9), _judgement(2, "none"))
            ),
            # c2: relation right but direction wrong (0.7), slot 2 wrong relation (0.5)
            _result(
                c2,
                _completion(
                    _judgement(1, "supports", confidence=0.7),
                    _judgement(2, "refines", confidence=0.5),
                ),
                prompt_tokens=2000,
                completion_tokens=300,
                reasoning_tokens=150,
                latency_ms=1500.0,
            ),
        ]
        price = ModelPrice(input_per_m=1.0, output_per_m=10.0)
        m = compute_metrics([c1, c2], results, labels=labels, confidence_floor=0.6, price=price)
        assert m["n_calls"] == 2
        assert m["rates"]["ok"] == 1.0
        assert m["contract_violation_rate"] == 0.0
        lab = m["labels"]
        assert lab["n_slots"] == 4
        assert lab["relation_agreement"] == pytest.approx(3 / 4)
        assert lab["n_directed"] == 2
        assert lab["direction_agreement"] == pytest.approx(1 / 2)
        assert lab["by_relation"]["supports"]["relation_agreement"] == 1.0
        assert lab["by_relation"]["contradicts"]["relation_agreement"] == 0.0
        assert lab["calibration"][">=0.8"] == {"n": 1, "agreement": 1.0}
        assert lab["calibration"]["0.6-0.8"] == {"n": 1, "agreement": 1.0}
        assert lab["calibration"]["<0.6"] == {"n": 1, "agreement": 0.0}
        ref = m["reference"]
        assert ref["n_slots"] == 4  # every candidate slot of both cases
        # reference: c1 slot1 supports f2c (match), slot2 none (match), c2 slot1 match, slot2 none vs refines (miss)
        assert ref["relation_agreement"] == pytest.approx(3 / 4)
        assert ref["direction_agreement"] == 1.0  # both directed matches agree with the reference
        assert ref["reference_models"] == ["ref"]
        # edges at floor 0.6: c1 → 1 (0.9), c2 → 1 (0.7; 0.5 is below)
        assert m["edges_per_call_at_floor"] == 1.0
        assert m["tokens"]["prompt_mean"] == 1500
        assert m["tokens"]["completion_mean"] == 200
        assert m["tokens"]["reasoning_share"] == pytest.approx(150 / 300)
        assert m["tokens"]["estimated_share"] == 0.0
        assert m["latency_ms"]["p50"] == 1000.0
        assert m["latency_ms"]["p95"] == 1500.0
        # (1500 * 1 + 200 * 10) / 1e6 * 1000
        assert m["cost_per_1k_calls"] == pytest.approx(3.5)

    def test_unusable_results_count_as_misses_and_rates(self) -> None:
        c1 = _case("c1")
        labels = {
            ("c1", 1): label_from_dict(
                {
                    "case_id": "c1",
                    "candidate_index": 1,
                    "relation": "none",
                    "labeller": "d",
                    "labelled_at": "x",
                }
            ),
        }
        results = [
            _result(c1, "garbage"),
            CaseResult("c2", "error", 2, None, 0, 0, None, False, 10.0, None, error="boom"),
        ]
        m = compute_metrics([c1, _case("c2")], results, labels=labels, confidence_floor=0.6)
        assert m["labels"]["relation_agreement"] == 0.0
        # every call errored → spend is unknown even when the model has a price
        all_errors = [CaseResult("c1", "error", 2, None, 0, 0, None, False, 1.0, None, error="x")]
        priced = compute_metrics([c1], all_errors, confidence_floor=0.6, price=ModelPrice(1.0, 1.0))
        assert priced["cost_per_1k_calls"] is None
        assert m["rates"]["parse_error"] == 0.5
        assert m["rates"]["error"] == 0.5
        assert m["contract_violation_rate"] is None  # nothing parsed
        assert m["cost_per_1k_calls"] is None
        assert m["tokens"]["reasoning_share"] is None

    def test_no_labels_and_empty_reference(self) -> None:
        c1 = _case("c1")
        m = compute_metrics([c1], [_result(c1, _completion())], confidence_floor=0.6)
        assert m["labels"] == {"n_slots": 0}
        assert m["reference"]["n_slots"] == 0
        assert m["rates"]["empty"] == 1.0
        assert m["contract_violation_rate"] == 1.0


class TestHelpers:
    def test_mean_and_percentile(self) -> None:
        assert mean([]) == 0.0
        assert percentile([], 0.5) == 0.0
        assert percentile([1, 5, 3], 0.5) == 3
        assert percentile([1, 2, 3, 4, 100], 0.95) == 100


class TestPrices:
    def test_load_and_cost(self, tmp_path: Path) -> None:
        path = tmp_path / "prices.toml"
        path.write_text(
            '[models."a/b"]\ninput_per_m = 0.25\noutput_per_m = 2.0\ncached_input_per_m = 0.025\nsource = "x"\n'
            '[models."free"]\ninput_per_m = 0\noutput_per_m = 0\n'
        )
        prices = load_prices(path)
        assert prices["a/b"].cached_input_per_m == 0.025
        assert prices["free"].cached_input_per_m is None
        assert cost_per_1k_calls(
            prices["a/b"], prompt_tokens_mean=2000, completion_tokens_mean=500
        ) == pytest.approx(1.5)
        assert cost_per_1k_calls(None, prompt_tokens_mean=1, completion_tokens_mean=1) is None

    def test_bad_tables(self, tmp_path: Path) -> None:
        path = tmp_path / "prices.toml"
        path.write_text('[models."a"]\ninput_per_m = "x"\noutput_per_m = 1\n')
        with pytest.raises(ValueError, match=r"models\.a"):
            load_prices(path)
        path.write_text("models = 3\n")
        with pytest.raises(ValueError, match="table"):
            load_prices(path)

    def test_committed_table_loads(self) -> None:
        prices = load_prices(Path("evals/prices.toml"))
        assert "openai/gpt-6-luna" in prices and "openai/gpt-5-mini" in prices
