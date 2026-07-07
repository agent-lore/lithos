"""Enforce the hard architecture budgets declared in docs/architecture.toml.

Each ``[budgets]`` entry is a ceiling on a measured structural metric. A breach
means the change made the architecture measurably worse: either undo the
structural change, or — if the regression is a deliberate, discussed tradeoff —
raise the budget in ``docs/architecture.toml`` in the same PR so the decision
is visible in review. Lower budgets after improving the code to lock in gains.
"""

from __future__ import annotations

import pytest

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
        "Known keys are defined in tests/guardrail/_metrics_toolkit.py::budget_actual."
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
    assert actual <= budget, (
        f"Architecture budget breached: {key} = {actual}, budget = {budget}.\n"
        f"Either revert the structural change that caused this, or — if it is a\n"
        f"deliberate tradeoff — raise `{key}` in docs/architecture.toml [budgets]\n"
        f"in this PR so the decision is reviewable. See docs/generated/metrics.md."
    )
