"""Promotion rule (lithos.lcma.eval.compare): policy parsing, gate checks, report."""

from __future__ import annotations

from pathlib import Path

import pytest

from lithos.lcma.eval.compare import (
    DEFAULT_POLICY,
    agreement_basis,
    comparison_rows,
    format_comparison,
    gate,
    load_policy,
)


def _metrics(
    *,
    relation=0.80,
    direction=0.90,
    cost=1.0,
    rates=None,
    basis="labels",
    n_slots=100,
):
    m = {
        "n_calls": 10,
        "rates": {"ok": 1.0, "partial": 0.0, "empty": 0.0, "parse_error": 0.0, "error": 0.0},
        "contract_violation_rate": 0.0,
        "labels": {"n_slots": 0},
        "reference": {"n_slots": 0},
        "edges_per_call_at_floor": 1.0,
        "tokens": {"prompt_mean": 1000, "completion_mean": 100, "reasoning_share": None},
        "latency_ms": {"p50": 1.0, "p95": 2.0},
        "cost_per_1k_calls": cost,
    }
    m[basis] = {
        "n_slots": n_slots,
        "relation_agreement": relation,
        "direction_agreement": direction,
    }
    if rates:
        m["rates"].update(rates)
    return m


class TestGate:
    def test_cheaper_and_as_good_is_promotable(self) -> None:
        result = gate(_metrics(cost=1.0), _metrics(cost=0.5))
        assert result.promotable
        assert result.basis == "labels"
        assert all(c.passed for c in result.checks)

    def test_agreement_drop_beyond_threshold_fails(self) -> None:
        result = gate(_metrics(relation=0.80), _metrics(relation=0.76, cost=0.1))
        assert not result.promotable
        assert [c.name for c in result.checks if not c.passed] == ["relation_agreement"]
        # exactly at the threshold passes
        assert gate(_metrics(relation=0.80), _metrics(relation=0.77, cost=0.1)).promotable

    def test_direction_drop_and_missing_direction(self) -> None:
        assert not gate(_metrics(direction=0.90), _metrics(direction=0.84, cost=0.1)).promotable
        assert gate(_metrics(direction=0.90), _metrics(direction=0.85, cost=0.1)).promotable
        result = gate(_metrics(direction=None), _metrics(direction=None, cost=0.1))
        assert result.promotable
        assert "no directed slots" in next(
            c.detail for c in result.checks if c.name == "direction_agreement"
        )

    def test_any_failure_rate_above_incumbent_fails(self) -> None:
        result = gate(_metrics(), _metrics(cost=0.1, rates={"parse_error": 0.01}))
        assert not result.promotable
        assert [c.name for c in result.checks if not c.passed] == ["rate:parse_error"]

    def test_cost_rule(self) -> None:
        # 30% cheaper at equal agreement passes; 20% cheaper does not
        assert gate(_metrics(cost=1.0), _metrics(cost=0.70)).promotable
        assert not gate(_metrics(cost=1.0), _metrics(cost=0.80)).promotable
        # equal cost with strictly higher agreement passes
        assert gate(_metrics(relation=0.80, cost=1.0), _metrics(relation=0.81, cost=1.0)).promotable
        assert not gate(
            _metrics(relation=0.80, cost=1.0), _metrics(relation=0.80, cost=1.0)
        ).promotable
        # unknown cost is a documented fail, not a crash
        result = gate(_metrics(cost=None), _metrics(cost=0.1))
        assert not result.promotable
        assert "prices.toml" in result.checks[-1].detail
        # free local model vs free incumbent
        assert gate(_metrics(cost=0.0), _metrics(cost=0.0)).promotable

    def test_reference_basis_when_labels_missing(self) -> None:
        inc = _metrics(basis="reference")
        cand = _metrics(basis="reference", cost=0.1)
        assert agreement_basis(inc, cand) == "reference"
        assert gate(inc, cand).basis == "reference"
        # labels on one side only → reference basis
        assert agreement_basis(_metrics(basis="labels"), cand) == "reference"

    def test_no_agreement_anywhere_fails_cleanly(self) -> None:
        inc = _metrics(basis="reference", n_slots=0, relation=None, direction=None)
        inc["reference"] = {"n_slots": 0}
        result = gate(inc, _metrics(cost=0.1))
        assert not result.promotable
        assert "n/a" in result.checks[0].detail

    def test_policy_overrides(self) -> None:
        policy = {**DEFAULT_POLICY, "cost_min_reduction": 0.10}
        assert gate(_metrics(cost=1.0), _metrics(cost=0.85), policy).promotable


class TestPolicyFile:
    def test_committed_policy_loads(self) -> None:
        policy = load_policy(Path("evals/POLICY.md"))
        assert policy["relation_agreement_max_drop"] == 0.03
        assert policy["direction_agreement_max_drop"] == 0.05
        assert policy["cost_min_reduction"] == 0.30
        assert policy["failure_outcomes"] == ["partial", "empty", "parse_error", "error"]

    def test_missing_block_and_bad_values(self, tmp_path: Path) -> None:
        path = tmp_path / "POLICY.md"
        path.write_text("# no block\n")
        with pytest.raises(ValueError, match="toml block"):
            load_policy(path)
        path.write_text("```toml\nrelation_agreement_max_drop = 3\n```\n")
        with pytest.raises(ValueError, match="relation_agreement_max_drop"):
            load_policy(path)
        path.write_text("```toml\nfailure_outcomes = 'x'\n```\n")
        with pytest.raises(ValueError, match="failure_outcomes"):
            load_policy(path)
        path.write_text("```toml\ncost_min_reduction = 0.5\n```\n")
        assert load_policy(path)["cost_min_reduction"] == 0.5
        assert load_policy(path)["relation_agreement_max_drop"] == 0.03  # default kept


class TestReport:
    def test_rows_and_markdown(self) -> None:
        a = {"run_id": "A", "metrics": _metrics()}
        b = {"run_id": "B", "metrics": _metrics(cost=0.5, relation=0.79)}
        names = [name for name, _, _ in comparison_rows(a["metrics"], b["metrics"])]
        assert names[0] == "calls" and names[-1] == "cost_per_1k_calls"
        assert "labels.relation_agreement" in names and "rate.parse_error" in names
        text = format_comparison(a, b, gate(a["metrics"], b["metrics"]))
        assert text.startswith("| metric | A | B |")
        assert "| labels.relation_agreement | 0.800 | 0.790 |" in text
        assert "**PROMOTABLE**" in text
        assert "- pass: cost_per_1k_calls" in text
        assert "n/a" in format_comparison(a, b)  # reference agreement absent
