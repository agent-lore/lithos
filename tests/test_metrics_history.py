"""Unit tests for scripts/metrics_history.py pure functions.

``iter_snapshots`` is git-backed and covered by the make target; here we test the
pure extraction/rendering over synthetic snapshots, including empty history.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import metrics_history as mh

_SNAP_A = ("aaaaaaa", "2026-01-01", {"graph": {"cross_component_edges": 63}, "items": [1, 2]})
_SNAP_B = ("bbbbbbb", "2026-02-01", {"graph": {"cross_component_edges": 60}, "items": [1, 2, 3]})


def test_extract_scalar_and_list_count() -> None:
    _, _, metrics = _SNAP_B
    assert mh.extract(metrics, "graph.cross_component_edges") == 60
    assert mh.extract(metrics, "items.count") == 3


def test_extract_missing_key_is_none() -> None:
    _, _, metrics = _SNAP_A
    assert mh.extract(metrics, "graph.nope") is None
    assert mh.extract(metrics, "absent.path") is None


def test_emit_csv_shape() -> None:
    csv = mh.emit_csv([_SNAP_A, _SNAP_B], ["graph.cross_component_edges", "items.count"])
    lines = csv.strip().splitlines()
    assert lines[0] == "sha,date,graph.cross_component_edges,items.count"
    assert lines[1] == "aaaaaaa,2026-01-01,63,2"
    assert lines[2] == "bbbbbbb,2026-02-01,60,3"


def test_emit_csv_missing_value_is_blank() -> None:
    csv = mh.emit_csv([_SNAP_A], ["graph.nope"])
    assert csv.strip().splitlines()[1] == "aaaaaaa,2026-01-01,"


def test_emit_mermaid_has_chart_per_key() -> None:
    out = mh.emit_mermaid([_SNAP_A, _SNAP_B], ["graph.cross_component_edges"])
    assert "xychart-beta" in out
    assert "## graph.cross_component_edges" in out
    assert 'x-axis ["aaaaaaa", "bbbbbbb"]' in out
    assert "line [63, 60]" in out


def test_emit_mermaid_missing_value_is_zero() -> None:
    out = mh.emit_mermaid([_SNAP_A], ["graph.nope"])
    assert "line [0]" in out


def test_emit_handles_empty_history() -> None:
    assert mh.emit_csv([], ["k"]) == "sha,date,k\n"
    assert mh.emit_mermaid([], ["k"]).startswith("## k")
