"""Unit tests for scripts/metrics_history.py.

The pure extraction/rendering helpers are tested over synthetic snapshots. The
git-backed ``iter_snapshots`` walker is tested with ``_git`` monkeypatched to
exercise normal history, commits missing the snapshot, and malformed snapshots —
so the walk's parse/skip logic is covered without a real repo.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

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


def _fake_git(log_out: str, shows: Mapping[str, object]) -> Callable[..., str]:
    """Stand-in for ``_git``: ``log`` returns ``log_out``; ``show <sha>:PATH``
    returns ``shows[sha]`` (a str), or raises it if it is an Exception."""

    def fake(*args: str) -> str:
        if args[0] == "log":
            return log_out
        if args[0] == "show":
            sha = args[1].split(":", 1)[0]
            result = shows[sha]
            if isinstance(result, Exception):
                raise result
            assert isinstance(result, str)
            return result
        raise AssertionError(f"unexpected git args: {args}")

    return fake


def test_iter_snapshots_parses_first_parent_history(monkeypatch: pytest.MonkeyPatch) -> None:
    log_out = "aaaaaaa 2026-01-01\nbbbbbbb 2026-02-01\n"
    shows = {
        "aaaaaaa": json.dumps({"graph": {"cross_component_edges": 63}}),
        "bbbbbbb": json.dumps({"graph": {"cross_component_edges": 60}}),
    }
    monkeypatch.setattr(mh, "_git", _fake_git(log_out, shows))
    snaps = mh.iter_snapshots()
    assert [(sha, date) for sha, date, _ in snaps] == [
        ("aaaaaaa", "2026-01-01"),
        ("bbbbbbb", "2026-02-01"),
    ]
    assert snaps[0][2]["graph"]["cross_component_edges"] == 63


def test_iter_snapshots_skips_commit_missing_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    log_out = "aaaaaaa 2026-01-01\nbbbbbbb 2026-02-01\n"
    shows = {
        "aaaaaaa": subprocess.CalledProcessError(128, "git show"),
        "bbbbbbb": json.dumps({"graph": {"cross_component_edges": 60}}),
    }
    monkeypatch.setattr(mh, "_git", _fake_git(log_out, shows))
    assert [sha for sha, _, _ in mh.iter_snapshots()] == ["bbbbbbb"]


def test_iter_snapshots_skips_malformed_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    log_out = "aaaaaaa 2026-01-01\nbbbbbbb 2026-02-01\n"
    shows = {
        "aaaaaaa": "{not valid json",
        "bbbbbbb": json.dumps({"graph": {"cross_component_edges": 60}}),
    }
    monkeypatch.setattr(mh, "_git", _fake_git(log_out, shows))
    assert [sha for sha, _, _ in mh.iter_snapshots()] == ["bbbbbbb"]


def test_iter_snapshots_empty_history(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mh, "_git", _fake_git("", {}))
    assert mh.iter_snapshots() == []
