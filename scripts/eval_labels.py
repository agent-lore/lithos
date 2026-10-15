#!/usr/bin/env python3
"""Labelling loop for a golden set: Markdown review sheet <-> labels.jsonl.

``sheet`` writes one section per (case, candidate) slot with the focus and
candidate text, the incumbent's recorded judgement, and a ``label:`` line
pre-filled from ``--proposed`` (or from the reference when no proposal is
given). The reviewer edits the ``label:`` lines in place; ``import`` parses
them back into ``labels.jsonl`` with the reviewer's name and today's date.

    python scripts/eval_labels.py sheet evals/edges/cases.jsonl --out review.md \
        [--proposed proposed.jsonl] [--case-ids ids.txt] [--limit 100]
    python scripts/eval_labels.py import review.md --labeller dave \
        --out evals/edges/labels.jsonl [--merge-into evals/edges/labels.jsonl]

``label:`` line grammar (one per slot, anything after ``#`` is a note)::

    label: <relation> [focus_to_candidate|candidate_to_focus] [low|medium|high] [rationale_ok=yes|no]

Relation ``none`` and the symmetric relations take no direction. A line
``label: skip`` leaves the slot unlabelled.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RELATIONS = {
    "supports",
    "contradicts",
    "refines",
    "is_example_of",
    "depends_on",
    "analogy_to",
    "none",
}
SYMMETRIC = {"contradicts", "analogy_to"}
DIRECTIONS = {"focus_to_candidate", "candidate_to_focus"}
CONFIDENCES = {"low", "medium", "high"}

_HEADER = re.compile(r"^## case (?P<case_id>\S+) · candidate (?P<index>\d+)\s*$")
_LABEL = re.compile(r"^label:\s*(?P<body>.*?)\s*$")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _clip(text: str, limit: int = 600) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _label_line(
    relation: str, direction: str | None, confidence: str, rationale_ok: bool | None
) -> str:
    parts = [relation]
    if relation not in SYMMETRIC and relation != "none" and direction:
        parts.append(direction)
    parts.append(confidence)
    if rationale_ok is not None:
        parts.append(f"rationale_ok={'yes' if rationale_ok else 'no'}")
    return "label: " + " ".join(parts)


def write_sheet(
    cases: list[dict[str, Any]],
    out: Path,
    *,
    proposed: dict[tuple[str, int], dict[str, Any]],
    case_ids: set[str] | None,
    limit: int | None,
) -> int:
    lines = [
        "# Golden-set review sheet",
        "",
        (
            "Edit the `label:` line under each slot. Grammar: `label: <relation> "
            + "[direction] [low|medium|high] [rationale_ok=yes|no]`; `label: skip` leaves it out."
        ),
        "",
    ]
    slots = 0
    for case in cases:
        if case_ids is not None and case["case_id"] not in case_ids:
            continue
        reference = (case.get("reference") or {}).get("judgements") or []
        ref_by_index = {int(j["candidate_index"]): j for j in reference}
        for candidate in case["candidates"]:
            if limit is not None and slots >= limit:
                break
            index = int(candidate["index"])
            slots += 1
            ref = ref_by_index.get(index)
            prop = proposed.get((case["case_id"], index))
            lines += [
                f"## case {case['case_id']} · candidate {index}",
                "",
                f"**Focus:** {case['focus'].get('title', '')}",
                f"> {_clip(case['focus'].get('content', ''))}",
                "",
                f"**Candidate [{index}]:** {candidate.get('title', '')} "
                f"(similarity {float(candidate.get('similarity', 0.0)):.2f})",
                f"> {_clip(candidate.get('snippet', ''))}",
                "",
            ]
            if ref is not None:
                direction = "" if ref["relation"] in SYMMETRIC else f" {ref.get('direction', '')}"
                lines.append(
                    f"Reference ({(case.get('reference') or {}).get('model', '?')}): "
                    f"{ref['relation']}{direction} {float(ref.get('confidence', 0)):.2f} — "
                    f'"{_clip(ref.get("rationale", ""), 200)}"'
                )
            else:
                lines.append("Reference: none")
            if prop is not None:
                lines.append(
                    _label_line(
                        prop["relation"],
                        prop.get("direction"),
                        prop.get("confidence", "medium"),
                        prop.get("reference_rationale_ok"),
                    )
                    + (f"  # {prop['notes']}" if prop.get("notes") else "")
                )
            elif ref is not None:
                lines.append(_label_line(ref["relation"], ref.get("direction"), "medium", None))
            else:
                lines.append(_label_line("none", None, "medium", None))
            lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return slots


def parse_sheet(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Labels from the edited sheet plus human-readable problems."""
    labels: list[dict[str, Any]] = []
    problems: list[str] = []
    current: tuple[str, int] | None = None
    for lineno, line in enumerate(text.splitlines(), start=1):
        header = _HEADER.match(line)
        if header:
            current = (header["case_id"], int(header["index"]))
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
        relation = tokens[0]
        if relation not in RELATIONS:
            problems.append(f"line {lineno}: unknown relation {relation!r}")
            continue
        direction: str | None = None
        confidence = "high"
        rationale_ok: bool | None = None
        for token in tokens[1:]:
            if token in DIRECTIONS:
                direction = token
            elif token in CONFIDENCES:
                confidence = token
            elif token.startswith("rationale_ok="):
                rationale_ok = token.split("=", 1)[1].lower() in {"yes", "true", "y"}
            else:
                problems.append(f"line {lineno}: unexpected token {token!r}")
        directed = relation not in SYMMETRIC and relation != "none"
        if directed and direction is None:
            problems.append(f"line {lineno}: {relation} needs a direction")
            continue
        labels.append(
            {
                "case_id": current[0],
                "candidate_index": current[1],
                "relation": relation,
                "direction": direction if directed else None,
                "confidence": confidence,
                "reference_rationale_ok": rationale_ok,
                "notes": note.strip(),
            }
        )
    return labels, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sheet = sub.add_parser("sheet", help="write the Markdown review sheet")
    sheet.add_argument("cases", type=Path)
    sheet.add_argument("--out", type=Path, required=True)
    sheet.add_argument(
        "--proposed", type=Path, default=None, help="labels.jsonl of proposed labels"
    )
    sheet.add_argument("--case-ids", type=Path, default=None, help="file with one case_id per line")
    sheet.add_argument("--limit", type=int, default=None, help="at most this many slots")
    imp = sub.add_parser("import", help="turn an edited sheet into labels.jsonl")
    imp.add_argument("sheet", type=Path)
    imp.add_argument("--labeller", required=True)
    imp.add_argument("--out", type=Path, required=True)
    imp.add_argument(
        "--merge-into",
        type=Path,
        default=None,
        help="existing labels.jsonl to keep other slots from",
    )
    imp.add_argument("--date", default=datetime.now(UTC).strftime("%Y-%m-%d"))
    args = parser.parse_args(argv)

    if args.command == "sheet":
        proposed: dict[tuple[str, int], dict[str, Any]] = {}
        if args.proposed is not None:
            for row in _read_jsonl(args.proposed):
                proposed[(row["case_id"], int(row["candidate_index"]))] = row
        case_ids = None
        if args.case_ids is not None:
            case_ids = {
                line.strip() for line in args.case_ids.read_text().splitlines() if line.strip()
            }
        slots = write_sheet(
            _read_jsonl(args.cases),
            args.out,
            proposed=proposed,
            case_ids=case_ids,
            limit=args.limit,
        )
        print(f"wrote {slots} slots to {args.out}")
        return 0

    labels, problems = parse_sheet(args.sheet.read_text(encoding="utf-8"))
    for problem in problems:
        print(f"warning: {problem}", file=sys.stderr)
    merged: dict[tuple[str, int], dict[str, Any]] = {}
    if args.merge_into is not None and args.merge_into.exists():
        for row in _read_jsonl(args.merge_into):
            merged[(row["case_id"], int(row["candidate_index"]))] = row
    for label in labels:
        merged[(label["case_id"], label["candidate_index"])] = {
            **label,
            "labeller": args.labeller,
            "labelled_at": args.date,
            "status": "reviewed",
        }
    rows = [merged[key] for key in sorted(merged)]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows),
        encoding="utf-8",
    )
    print(f"wrote {len(rows)} labels to {args.out} ({len(labels)} from the sheet)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
