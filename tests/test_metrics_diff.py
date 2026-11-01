"""Unit tests for the metrics-diff logic used by CI PR summaries.

Covers the pure diff/render helpers and the ``main()`` CLI wrapper — including
the empty-base branch that fires on a port's own first PR, before
``metrics.json`` exists on the base branch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import metrics_diff  # pyright: ignore[reportMissingImports]


def test_diff_reports_changed_scalars() -> None:
    old = {"graph": {"cross_component_edges": 63}, "size": {"total_sloc": 100}}
    new = {"graph": {"cross_component_edges": 64}, "size": {"total_sloc": 100}}

    changes = metrics_diff.diff_metrics(old, new)

    assert changes == [("graph.cross_component_edges", 63, 64)]


def test_diff_summarizes_lists_by_length() -> None:
    old = {"graph": {"component_cycles": [["A", "B"]]}}
    new = {"graph": {"component_cycles": [["A", "B"], ["C", "D"]]}}

    changes = metrics_diff.diff_metrics(old, new)

    assert ("graph.component_cycles.count", 1, 2) in changes
    assert any(key == "graph.component_cycles.items" for key, _, _ in changes)


def test_diff_tolerates_missing_sections() -> None:
    old: dict = {}
    new = {"mcp": {"tools": 37}}

    changes = metrics_diff.diff_metrics(old, new)

    assert changes == [("mcp.tools", None, 37)]


def test_diff_empty_when_identical() -> None:
    metrics = {
        "graph": {"cross_component_edges": 63},
        "size": {"modules_over_800": ["a", "b"]},
    }

    assert metrics_diff.diff_metrics(metrics, metrics) == []


def test_render_markdown_has_table_and_delta() -> None:
    changes = [("graph.cross_component_edges", 63, 64)]

    out = metrics_diff.render(changes, markdown=True)

    assert "| Metric | Base | Head | Δ |" in out
    assert "`graph.cross_component_edges`" in out
    assert "+1" in out


def test_render_markdown_no_changes() -> None:
    assert "no changes" in metrics_diff.render([], markdown=True)


def _write(path: Path, obj: object) -> Path:
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def test_main_empty_base_prints_first_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _write(tmp_path / "base.json", {})  # no base snapshot yet
    head = _write(tmp_path / "head.json", {"size": {"total_sloc": 100}})

    assert metrics_diff.main([str(base), str(head)]) == 0
    out = capsys.readouterr().out
    assert "no base to diff" in out.lower()
    assert "total_sloc" not in out  # not the full "— -> value" table


def test_main_empty_base_markdown(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _write(tmp_path / "base.json", {})
    head = _write(tmp_path / "head.json", {"size": {"total_sloc": 100}})

    assert metrics_diff.main([str(base), str(head), "--markdown"]) == 0
    assert capsys.readouterr().out.startswith("### Architecture metrics: first snapshot")


def test_main_with_base_emits_diff(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _write(tmp_path / "base.json", {"size": {"total_sloc": 100}})
    head = _write(tmp_path / "head.json", {"size": {"total_sloc": 110}})

    assert metrics_diff.main([str(base), str(head)]) == 0
    out = capsys.readouterr().out
    assert "size.total_sloc" in out
    assert "no base to diff" not in out.lower()


def test_same_length_lists_still_report_changed_membership() -> None:
    changes = metrics_diff.diff_metrics({"cycles": [["A", "B"]]}, {"cycles": [["C", "D"]]})
    assert len(changes) == 1
    assert changes[0][0] == "cycles.items"


def test_cli_enforces_explicit_changed_code_policy_and_accepts_bounded_exception(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.quality import policy_config

    metadata = {"definitions": {"complexity": 1}, "language": "python", "scope": {}, "tools": {}}
    old = {"measurement": metadata, "functions": {"pkg.f": {"complexity": 11, "length": 20}}}
    new = {
        "measurement": metadata,
        "functions": {"pkg.f": {"complexity": 18, "length": 20}},
        "review": policy_config({"review": {"enforce_worsened": True}}),
    }
    base, head = _write(tmp_path / "base.json", old), _write(tmp_path / "head.json", new)
    assert metrics_diff.main([str(base), str(head), "--check"]) == 1
    assert "worsened" in capsys.readouterr().out
    new["review"]["exceptions"] = [
        {
            "metric": "function_complexity",
            "target": "pkg.f",
            "limit": 18,
            "reason": "Cohesive parser",
            "review": "PR #123",
        }
    ]
    _write(head, new)
    assert metrics_diff.main([str(base), str(head), "--check", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["violations"][0]["accepted"]
