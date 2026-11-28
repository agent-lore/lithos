#!/usr/bin/env python3
"""Labelling loop for the retrieval golden set: Markdown review sheet <-> labels.jsonl.

``sheet`` writes one section per pooled (query, note) pair: the query and
its caller, the note's title, path and text head as the pool recorded them,
where each method ranked the note, and a ``label:`` line pre-filled from
``--proposed`` (or ``not`` when there is no proposal). The reviewer edits the
``label:`` lines; ``import`` parses them back into labels.jsonl.

    python scripts/eval_retrieval_labels.py sheet CASES.jsonl [CASES2.jsonl ...] --out review.md \\
        [--proposed proposed.jsonl] [--labels labels.jsonl] [--case-ids ids.txt] \\
        [--sample N --seed S] [--blind] [--limit N]
    python scripts/eval_retrieval_labels.py import review.md --labeller dave \\
        --out labels.jsonl [--merge-into labels.jsonl]

``--labels`` leaves out pairs that file already labels, so a second pass
covers only the new candidates of a re-pooled set. ``--sample`` draws that
many pairs at random (for a blind calibration pass); ``--blind`` hides the
proposals and the per-method ranks, so the sheet shows only the query and
the note.

``label:`` line grammar (one per pair; anything after ``#`` is a note)::

    label: relevant | related | not | skip

``relevant``: the note answers or directly bears on the query. ``related``: same
project or topic, the caller would see why it came back and might skim it,
but it does not answer the query. ``not``: anything else, including notes
that merely share the query's words. ``skip``
leaves the pair unlabelled.
"""

from __future__ import annotations

import argparse
import random
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lithos.lcma.eval.retrieval.cases import (
    GRADES,
    METHODS,
    RetrievalCase,
    load_retrieval_cases,
    read_jsonl,
    write_jsonl,
)

_HEADER = re.compile(r"^## case (?P<case_id>\S+) · note (?P<note_id>\S+)\s*$")
_LABEL = re.compile(r"^label:\s*(?P<body>.*?)\s*$")

Slot = tuple[str, str]


def _ranks(case: RetrievalCase, note_id: str) -> str:
    parts = []
    for method in METHODS:
        run = case.methods.get(method)
        if run is None:
            continue
        ranked = next((r for r in run.results if r.id == note_id), None)
        parts.append(f"{method} #{ranked.rank} ({ranked.score:.3f})" if ranked else f"{method} -")
    return ", ".join(parts)


def select_slots(
    cases: list[RetrievalCase],
    *,
    exclude: set[Slot],
    case_ids: set[str] | None,
    sample: int | None,
    seed: int,
    limit: int | None,
) -> list[tuple[RetrievalCase, str]]:
    slots = [
        (case, note_id)
        for case in cases
        if case_ids is None or case.case_id in case_ids
        for note_id in case.pooled_ids()
        if (case.case_id, note_id) not in exclude
    ]
    if sample is not None:
        slots = random.Random(seed).sample(slots, min(sample, len(slots)))
    if limit is not None:
        slots = slots[:limit]
    return slots


_SHEET_HOW_TO = (
    "Edit the `label:` line under each pair: `relevant`, `related`, `not`, or `skip`. "
    "Anything after `#` is kept as a note."
)
_SHEET_GRADES = (
    "`relevant`: the note answers or directly bears on the query; the caller would be glad "
    "to have it. `related`: same project or topic, the caller would see why it came back "
    "and might skim it, but it does not answer the query. `not`: anything else, including "
    "notes that merely share the query's words."
)


def write_sheet(
    slots: list[tuple[RetrievalCase, str]],
    out: Path,
    *,
    proposed: dict[Slot, dict[str, Any]],
    blind: bool,
) -> int:
    lines = ["# Retrieval golden-set review sheet", "", _SHEET_HOW_TO, "", _SHEET_GRADES, ""]
    for case, note_id in slots:
        note = case.notes.get(note_id) or {}
        lines += [
            f"## case {case.case_id} · note {note_id}",
            "",
            f"**Query** ({case.caller}, slice {case.slice}): {case.query}",
            "",
            f"**Note:** {note.get('title') or '(missing)'} — `{note.get('path') or '?'}`",
        ]
        if not blind:
            lines.append(f"Ranked: {_ranks(case, note_id)}")
        lines += ["", f"> {note.get('text') or '(no text recorded)'}", ""]
        prop = None if blind else proposed.get((case.case_id, note_id))
        if prop is not None:
            lines.append(
                f"label: {prop['grade']}" + (f"  # {prop['notes']}" if prop.get("notes") else "")
            )
        else:
            lines.append("label: not")
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return len(slots)


def parse_sheet(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Labels from the edited sheet plus human-readable problems."""
    labels: list[dict[str, Any]] = []
    problems: list[str] = []
    current: Slot | None = None
    for lineno, line in enumerate(text.splitlines(), start=1):
        header = _HEADER.match(line)
        if header:
            current = (header["case_id"], header["note_id"])
            continue
        match = _LABEL.match(line)
        if not match:
            continue
        if current is None:
            problems.append(f"line {lineno}: label before any case header")
            continue
        body, _, note = match["body"].partition("#")
        tokens = body.split()
        if not tokens or tokens[0] == "skip":
            continue
        grade = tokens[0]
        if grade not in GRADES or len(tokens) > 1:
            problems.append(
                f"line {lineno}: expected one of {sorted(GRADES)} or skip, got {body!r}"
            )
            continue
        labels.append(
            {"case_id": current[0], "note_id": current[1], "grade": grade, "notes": note.strip()}
        )
    return labels, problems


def _read_slots(path: Path | None) -> dict[Slot, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    return {(row["case_id"], row["note_id"]): row for row in read_jsonl(path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sheet = sub.add_parser("sheet", help="write the Markdown review sheet")
    sheet.add_argument("cases", type=Path, nargs="+")
    sheet.add_argument("--out", type=Path, required=True)
    sheet.add_argument("--proposed", type=Path, default=None, help="jsonl of proposed labels")
    sheet.add_argument(
        "--labels", type=Path, default=None, help="labels.jsonl whose pairs are left out"
    )
    sheet.add_argument("--case-ids", type=Path, default=None, help="file with one case_id per line")
    sheet.add_argument("--sample", type=int, default=None, help="draw this many pairs at random")
    sheet.add_argument("--seed", type=int, default=0)
    sheet.add_argument("--blind", action="store_true", help="hide proposals and method ranks")
    sheet.add_argument("--limit", type=int, default=None, help="at most this many pairs")
    imp = sub.add_parser("import", help="turn an edited sheet into labels.jsonl")
    imp.add_argument("sheet", type=Path)
    imp.add_argument("--labeller", required=True)
    imp.add_argument("--out", type=Path, required=True)
    imp.add_argument(
        "--merge-into",
        type=Path,
        default=None,
        help="existing labels.jsonl to keep other pairs from",
    )
    imp.add_argument("--status", default="reviewed", help="reviewed (default) or proposed")
    imp.add_argument("--date", default=datetime.now(UTC).strftime("%Y-%m-%d"))
    args = parser.parse_args(argv)

    if args.command == "sheet":
        case_ids = None
        if args.case_ids is not None:
            case_ids = {
                line.strip() for line in args.case_ids.read_text().splitlines() if line.strip()
            }
        slots = select_slots(
            load_retrieval_cases(args.cases),
            exclude=set(_read_slots(args.labels)),
            case_ids=case_ids,
            sample=args.sample,
            seed=args.seed,
            limit=args.limit,
        )
        n = write_sheet(slots, args.out, proposed=_read_slots(args.proposed), blind=args.blind)
        print(f"wrote {n} pairs to {args.out}")
        return 0

    labels, problems = parse_sheet(args.sheet.read_text(encoding="utf-8"))
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    merged = _read_slots(args.merge_into)
    for label in labels:
        merged[(label["case_id"], label["note_id"])] = {
            **label,
            "labeller": args.labeller,
            "labelled_at": args.date,
            "status": args.status,
        }
    rows = [merged[slot] for slot in sorted(merged)]
    write_jsonl(args.out, rows)
    print(f"{len(labels)} labels imported; {len(rows)} in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
