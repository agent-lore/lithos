"""scripts/eval_retrieval_labels.py — review sheet round trip."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import eval_retrieval_labels  # noqa: E402

SEED = ROOT / "evals" / "retrieval" / "seed"


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_sheet_then_import_round_trip(tmp_path: Path) -> None:
    sheet = tmp_path / "review.md"
    assert (
        eval_retrieval_labels.main(["sheet", str(SEED / "cases.jsonl"), "--out", str(sheet)]) == 0
    )
    text = sheet.read_text()
    assert text.count("## case ") == 8  # every pooled pair of the seed set
    assert "Ranked: hybrid #1 (0.032), semantic #1 (0.610), retrieve #2 (0.380)" in text
    assert text.count("label: not") == 8  # no proposals: pre-filled as not
    text = text.replace("label: not", "label: relevant  # the benchmark", 1)
    text = text.replace("label: not", "label: skip", 1)
    sheet.write_text(text)
    out = tmp_path / "labels.jsonl"
    assert (
        eval_retrieval_labels.main(
            ["import", str(sheet), "--labeller", "dave", "--out", str(out), "--date", "2026-11-23"]
        )
        == 0
    )
    rows = _rows(out)
    assert len(rows) == 7  # eight pairs, one skipped
    first = next(r for r in rows if r["note_id"] == "seed-note-1" and r["case_id"] == "seed-001")
    assert first["grade"] == "relevant"
    assert first["notes"] == "the benchmark"
    assert first["labeller"] == "dave" and first["status"] == "reviewed"


def test_import_rejects_bad_grammar(tmp_path: Path, capsys) -> None:
    sheet = tmp_path / "review.md"
    sheet.write_text("## case seed-001 · note seed-note-1\n\nlabel: maybe\n")
    assert (
        eval_retrieval_labels.main(
            ["import", str(sheet), "--labeller", "d", "--out", str(tmp_path / "l.jsonl")]
        )
        == 1
    )
    assert "expected one of" in capsys.readouterr().err


def test_merge_keeps_other_pairs(tmp_path: Path) -> None:
    sheet = tmp_path / "review.md"
    sheet.write_text("## case seed-002 · note seed-note-5\n\nlabel: superseded\n")
    out = tmp_path / "labels.jsonl"
    assert (
        eval_retrieval_labels.main(
            [
                "import",
                str(sheet),
                "--labeller",
                "d",
                "--out",
                str(out),
                "--merge-into",
                str(SEED / "labels.jsonl"),
            ]
        )
        == 0
    )
    rows = _rows(out)
    assert len(rows) == 8
    assert next(r for r in rows if r["note_id"] == "seed-note-5")["grade"] == "superseded"


def test_proposed_blind_sample_and_exclusion(tmp_path: Path) -> None:
    proposed = tmp_path / "proposed.jsonl"
    proposed.write_text(
        json.dumps(
            {
                "case_id": "seed-001",
                "note_id": "seed-note-3",
                "grade": "superseded",
                "notes": "older run",
            }
        )
        + "\n"
    )
    sheet = tmp_path / "review.md"
    eval_retrieval_labels.main(
        ["sheet", str(SEED / "cases.jsonl"), "--out", str(sheet), "--proposed", str(proposed)]
    )
    assert "label: superseded  # older run" in sheet.read_text()

    eval_retrieval_labels.main(
        [
            "sheet",
            str(SEED / "cases.jsonl"),
            "--out",
            str(sheet),
            "--proposed",
            str(proposed),
            "--blind",
            "--sample",
            "3",
            "--seed",
            "1",
        ]
    )
    blind = sheet.read_text()
    assert blind.count("## case ") == 3
    assert "Ranked:" not in blind and "older run" not in blind

    eval_retrieval_labels.main(
        [
            "sheet",
            str(SEED / "cases.jsonl"),
            "--out",
            str(sheet),
            "--labels",
            str(SEED / "labels.jsonl"),
        ]
    )
    assert sheet.read_text().count("## case ") == 0  # everything is already labelled
