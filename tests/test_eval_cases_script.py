"""scripts/eval_cases.py: attach prompt-v2 note views and mirror directed labels."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import eval_cases  # noqa: E402

from lithos.lcma.eval.cases import case_from_dict, label_from_dict  # noqa: E402

NOTE = """---
id: {id}
title: {title}
author: influx
created_at: '{created}'
updated_at: '{created}'
version: 3
source_url: https://www.example.org/{id}
tags:
- feed-slug:example-feed
---

# {title}

## Summary
### Contributions
- {title} finding

## Full Text
Body.
"""


def _vault(root: Path, notes: dict[str, tuple[str, str]]) -> Path:
    for node_id, (title, created) in notes.items():
        (root / f"{node_id}.md").write_text(NOTE.format(id=node_id, title=title, created=created))
    return root


def _case(case_id: str = "c1") -> dict:
    return {
        "case_id": case_id,
        "focus": {"id": "f", "title": "Focus", "content": "# Focus\n\nas sent"},
        "candidates": [
            {"index": 1, "node_id": "a", "title": "A", "snippet": "chunk a", "similarity": 0.6},
            {"index": 2, "node_id": "b", "title": "B", "snippet": "chunk b", "similarity": 0.5},
        ],
        "messages": None,
        "prompt_version": None,
        "snippet_chars": 700,
        "reference": None,
    }


def _label(index: int, relation: str, direction: str | None) -> dict:
    return {
        "case_id": "c1",
        "candidate_index": index,
        "relation": relation,
        "direction": direction,
        "confidence": "high",
        "labeller": "dave",
        "labelled_at": "2026-11-16",
        "status": "reviewed",
    }


def test_attach_notes_adds_views_and_keeps_v1_text(tmp_path: Path) -> None:
    vault = _vault(
        tmp_path,
        {
            "f": ("Focus", "2026-10-15T00:00:00+00:00"),
            "a": ("A", "2026-09-01T00:00:00+00:00"),
            "b": ("B", "2026-08-01T00:00:00+00:00"),
        },
    )
    cases = [_case()]
    attached, missing = eval_cases.attach_notes(cases, [("prod", vault)], snippet_chars=1200)
    assert (attached, missing) == (3, [])
    case = cases[0]
    assert case["focus"]["content"] == "# Focus\n\nas sent"  # v1 text untouched
    assert case["candidates"][0]["snippet"] == "chunk a"
    assert (case["snippet_chars"], case["note_vault"]) == (1200, "prod")
    assert case["note_versions"] == {"f": 3, "a": 3, "b": 3}
    note = case["candidates"][0]["note"]
    assert note["created_at"].startswith("2026-09-01") and note["feed"] == "example-feed"
    assert "Full Text" not in note["content"]
    assert case_from_dict(case).neighbours()[0].note is not None


def test_attach_notes_reports_missing_notes(tmp_path: Path) -> None:
    vault = _vault(tmp_path, {"f": ("Focus", "2026-10-15T00:00:00+00:00")})
    _, missing = eval_cases.attach_notes([_case()], [("prod", vault)], snippet_chars=1200)
    assert sorted(missing) == ["a", "b"]


def test_mirror_swaps_notes_and_flips_scored_directions_only() -> None:
    labels = [
        _label(1, "supports", "focus_to_candidate"),
        _label(2, "refines", "either"),  # direction unscored: not mirrored
    ]
    cases, out_labels = eval_cases.mirror_cases([_case()], labels, "2026-11-18")
    mirrors = [c for c in cases if c["case_id"].startswith("mirror-")]
    assert [c["case_id"] for c in mirrors] == ["mirror-01"]
    mirror = case_from_dict(mirrors[0])
    assert (mirror.focus["id"], mirror.focus["title"]) == ("a", "A")
    assert [(n.node_id, n.title, n.snippet) for n in mirror.neighbours()] == [
        ("f", "Focus", "# Focus\n\nas sent")
    ]
    assert "positive:mirror" in mirror.tags
    label = label_from_dict(out_labels[-1])
    assert (label.case_id, label.relation, label.direction) == (
        "mirror-01",
        "supports",
        "candidate_to_focus",
    )


def test_mirror_skips_excluded_slots(tmp_path: Path) -> None:
    exclude = tmp_path / "exclude.txt"
    exclude.write_text("# evidence only in the candidate's passage\nc1/1  # reason\n\n")
    labels = [
        _label(1, "supports", "focus_to_candidate"),
        _label(2, "refines", "focus_to_candidate"),
    ]
    cases, _ = eval_cases.mirror_cases(
        [_case()], labels, "2026-11-18", eval_cases.read_exclusions(exclude)
    )
    assert [c["tags"][1] for c in cases if c["case_id"].startswith("mirror-")] == ["mirror-of:c1/2"]


def test_mirror_regenerates_rather_than_appends() -> None:
    labels = [_label(1, "supports", "focus_to_candidate")]
    cases, labels = eval_cases.mirror_cases([_case()], labels, "2026-11-18")
    cases, labels = eval_cases.mirror_cases(cases, labels, "2026-11-18")
    assert sum(c["case_id"].startswith("mirror-") for c in cases) == 1
    assert sum(lab["case_id"].startswith("mirror-") for lab in labels) == 1


def test_cli_round_trip(tmp_path: Path) -> None:
    (tmp_path / "vault").mkdir()
    vault = _vault(
        tmp_path / "vault",
        {
            "f": ("Focus", "2026-10-15T00:00:00+00:00"),
            "a": ("A", "2026-09-01T00:00:00+00:00"),
            "b": ("B", "2026-08-01T00:00:00+00:00"),
        },
    )
    cases = tmp_path / "cases.jsonl"
    labels = tmp_path / "labels.jsonl"
    cases.write_text(json.dumps(_case()) + "\n")
    labels.write_text(json.dumps(_label(1, "supports", "focus_to_candidate")) + "\n")
    assert eval_cases.main(["attach-notes", str(cases), "--vault", f"prod={vault}"]) == 0
    assert eval_cases.main(["mirror", str(cases), str(labels)]) == 0
    assert len(cases.read_text().splitlines()) == 2
    assert len(labels.read_text().splitlines()) == 2
