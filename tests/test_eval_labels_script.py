"""scripts/eval_labels.py — review sheet round trip."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import eval_labels  # noqa: E402

SEED = ROOT / "evals" / "edges" / "seed" / "cases.jsonl"


def test_sheet_then_import_round_trip(tmp_path: Path) -> None:
    sheet = tmp_path / "review.md"
    assert eval_labels.main(["sheet", str(SEED), "--out", str(sheet), "--limit", "3"]) == 0
    text = sheet.read_text()
    assert text.count("## case ") == 3
    assert "label: supports candidate_to_focus medium" in text  # pre-filled from the reference
    # Reviewer edits one slot and skips another.
    text = text.replace(
        "label: supports candidate_to_focus medium",
        "label: refines focus_to_candidate high rationale_ok=no  # sharpened",
        1,
    )
    text = text.replace("label: none medium", "label: skip", 1)
    sheet.write_text(text)
    out = tmp_path / "labels.jsonl"
    assert (
        eval_labels.main(
            ["import", str(sheet), "--labeller", "dave", "--out", str(out), "--date", "2026-10-20"]
        )
        == 0
    )
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 2
    edited = next(r for r in rows if r["relation"] == "refines")
    assert edited == {
        "case_id": "seed-001",
        "candidate_index": 1,
        "relation": "refines",
        "direction": "focus_to_candidate",
        "confidence": "high",
        "reference_rationale_ok": False,
        "notes": "sharpened",
        "labeller": "dave",
        "labelled_at": "2026-10-20",
        "status": "reviewed",
    }


def test_proposed_labels_and_merge(tmp_path: Path) -> None:
    proposed = tmp_path / "proposed.jsonl"
    proposed.write_text(
        json.dumps(
            {
                "case_id": "seed-002",
                "candidate_index": 2,
                "relation": "analogy_to",
                "confidence": "low",
                "notes": "guess",
            }
        )
        + "\n"
    )
    ids = tmp_path / "ids.txt"
    ids.write_text("seed-002\n")
    sheet = tmp_path / "review.md"
    eval_labels.main(
        [
            "sheet",
            str(SEED),
            "--out",
            str(sheet),
            "--proposed",
            str(proposed),
            "--case-ids",
            str(ids),
        ]
    )
    text = sheet.read_text()
    assert text.count("## case ") == 2
    assert "label: analogy_to low  # guess" in text
    existing = tmp_path / "existing.jsonl"
    existing.write_text(
        json.dumps(
            {
                "case_id": "zzz",
                "candidate_index": 1,
                "relation": "none",
                "direction": None,
                "confidence": "high",
                "reference_rationale_ok": None,
                "labeller": "x",
                "labelled_at": "2026-01-01",
                "status": "reviewed",
                "notes": "",
            }
        )
        + "\n"
    )
    out = tmp_path / "labels.jsonl"
    eval_labels.main(
        [
            "import",
            str(sheet),
            "--labeller",
            "dave",
            "--out",
            str(out),
            "--merge-into",
            str(existing),
        ]
    )
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert {r["case_id"] for r in rows} == {"seed-002", "zzz"}


def test_bad_lines_are_warnings_and_nonzero_exit(tmp_path: Path) -> None:
    sheet = tmp_path / "review.md"
    sheet.write_text(
        "label: supports\n## case a · candidate 1\nlabel: likes\n## case a · candidate 2\nlabel: supports\n## case a · candidate 3\nlabel: none weird\n"
    )
    labels, problems = eval_labels.parse_sheet(sheet.read_text())
    assert [row["case_id"] for row in labels] == ["a"] and labels[0]["relation"] == "none"
    assert len(problems) == 4
    out = tmp_path / "labels.jsonl"
    assert eval_labels.main(["import", str(sheet), "--labeller", "d", "--out", str(out)]) == 1
