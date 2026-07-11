"""Regenerate the architecture metrics snapshot (drift-checked in CI).

Writes ``docs/generated/metrics.json`` + ``metrics.md`` from one metrics dict.
Because the snapshot is committed and byte-identical for identical code, the
git history of ``metrics.json`` is the metric time series (see
``scripts/metrics_history.py``); the CI diff gate makes any structural change
show up as a reviewable metrics diff in the PR.
"""

from __future__ import annotations

import json

import pytest

from tests.guardrail import _metrics_render as render
from tests.guardrail import _metrics_toolkit as mt
from tests.guardrail._common import load_architecture, write


@pytest.fixture(scope="module")
def metrics() -> dict:
    return mt.compute_metrics()


def test_generate_metrics_snapshot(metrics: dict) -> None:
    budgets = load_architecture().get("budgets", {})
    json_out = write("metrics.json", render.render_metrics_json(metrics))
    md_out = write("metrics.md", render.render_metrics_md(metrics, budgets))
    assert json.loads(json_out.read_text(encoding="utf-8"))["schema"] == 1
    assert "# Architecture metrics" in md_out.read_text(encoding="utf-8")


def test_metrics_are_deterministic(metrics: dict) -> None:
    again = mt.compute_metrics()
    assert render.render_metrics_json(metrics) == render.render_metrics_json(again)


def test_every_component_has_metrics(metrics: dict) -> None:
    components = set(load_architecture()["components"])
    assert set(metrics["graph"]["components"]) == components
    assert set(metrics["size"]["components"]) == components
    assert set(metrics["complexity"]["components"]) == components


def test_metrics_sanity(metrics: dict) -> None:
    assert metrics["size"]["total_lines"] > 0
    assert metrics["size"]["total_sloc"] <= metrics["size"]["total_lines"]
    for comp, stats in metrics["graph"]["components"].items():
        assert 0.0 <= stats["instability"] <= 1.0, comp
    # The MCP tool surface is an optional adapter: assert a positive count only
    # when this project declares one, else it must be exactly zero.
    if load_architecture().get("tool_catalog", {}).get("include_modules"):
        assert metrics["mcp"]["tools"] > 0
    else:
        assert metrics["mcp"]["tools"] == 0
    assert metrics["domain"]["models"] > 0
