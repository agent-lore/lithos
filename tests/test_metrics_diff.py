"""Unit tests for the pure metrics-diff logic used by CI PR summaries."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import metrics_diff


def test_diff_reports_changed_scalars() -> None:
    old = {"graph": {"cross_component_edges": 63}, "size": {"total_sloc": 100}}
    new = {"graph": {"cross_component_edges": 64}, "size": {"total_sloc": 100}}

    changes = metrics_diff.diff_metrics(old, new)

    assert changes == [("graph.cross_component_edges", 63, 64)]


def test_diff_summarizes_lists_by_length() -> None:
    old = {"graph": {"component_cycles": [["A", "B"]]}}
    new = {"graph": {"component_cycles": [["A", "B"], ["C", "D"]]}}

    changes = metrics_diff.diff_metrics(old, new)

    assert changes == [("graph.component_cycles.count", 1, 2)]


def test_diff_tolerates_missing_sections() -> None:
    old: dict = {}
    new = {"mcp": {"tools": 37}}

    changes = metrics_diff.diff_metrics(old, new)

    assert changes == [("mcp.tools", None, 37)]


def test_diff_empty_when_identical() -> None:
    metrics = {"graph": {"cross_component_edges": 63}, "size": {"modules_over_800": ["a", "b"]}}

    assert metrics_diff.diff_metrics(metrics, metrics) == []


def test_render_markdown_has_table_and_delta() -> None:
    changes = [("graph.cross_component_edges", 63, 64)]

    out = metrics_diff.render(changes, markdown=True)

    assert "| Metric | Base | Head | Δ |" in out
    assert "`graph.cross_component_edges`" in out
    assert "+1" in out


def test_render_markdown_no_changes() -> None:
    assert "no changes" in metrics_diff.render([], markdown=True)
