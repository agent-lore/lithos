"""Enforce the accepted architecture budgets in docs/architecture.toml.

A breach requires a design decision, not necessarily a refactor. Architectural
invariants require an explicit architecture revision to change; review
thresholds can be adjusted when keeping cohesive code together is clearer.
See AGENTS.md for the policy and document adjustments in the same PR.
"""

from __future__ import annotations

import pytest
from scripts.quality import policy_config, policy_errors, policy_mode, violations

from tests.guardrail import _metrics_toolkit as mt
from tests.guardrail._common import load_architecture

_BUDGETS: dict[str, int] = load_architecture().get("budgets", {})


@pytest.fixture(scope="module")
def metrics() -> dict:
    return mt.compute_metrics()


def test_budgets_are_declared() -> None:
    assert _BUDGETS, "docs/architecture.toml is missing its [budgets] section"


def test_budget_keys_are_known(metrics: dict) -> None:
    """A typo in a budget key must fail loudly, not silently disable the ratchet."""
    unknown = [key for key in _BUDGETS if not _is_known(metrics, key)]
    assert not unknown, (
        f"Unknown [budgets] keys in docs/architecture.toml: {unknown}. "
        + "Known keys are defined in tests/guardrail/_metrics_toolkit.py::budget_actual."
    )


def _is_known(metrics: dict, key: str) -> bool:
    try:
        mt.budget_actual(metrics, key)
    except KeyError:
        return False
    return True


@pytest.mark.parametrize("key", sorted(_BUDGETS))
def test_budgets_hold(metrics: dict, key: str) -> None:
    actual = mt.budget_actual(metrics, key)
    budget = _BUDGETS[key]
    config = policy_config(load_architecture())
    if policy_mode(config, key) != "invariant":
        return  # review/context budgets are reported, not hard gates
    assert key in metrics["review"]["actuals"], f"Invariant {key} is unsupported for this language"
    assert actual <= budget, (
        f"Architecture budget breached: {key} = {actual}, budget = {budget}.\n"
        + "For an architectural invariant, preserve the rule or explicitly justify\n"
        + "an architecture revision in this PR. For a review threshold, refactor\n"
        + "only where there is a clean seam that improves clarity or cohesion.\n"
        + "Do not split files or extract helpers solely to satisfy a number.\n"
        + "If keeping the code together is clearer, adjust the relevant threshold\n"
        + "in docs/architecture.toml [budgets] and explain the affected code and\n"
        + "design tradeoff in this PR. See tests/guardrail/AGENTS.md and\n"
        + "docs/generated/metrics.md. CI enforces the accepted configuration."
    )


def test_policy_configuration(metrics: dict) -> None:
    assert not policy_errors(metrics["review"]), "\n".join(policy_errors(metrics["review"]))


def test_entity_invariants(metrics: dict) -> None:
    broken = [r for r in violations(metrics) if r["policy"] == "invariant"]
    assert not broken, f"Architectural invariants breached: {broken}"
