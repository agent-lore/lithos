#!/usr/bin/env python3
"""Maintain the typed-edge golden set (``evals/edges/``).

``attach-notes`` gives every focus and candidate its prompt-v2 view (date,
source and summary head, :mod:`lithos.lcma.note_view`) read from the vault
data dirs the cases were exported from, and sets the case's text budget. The
prompt-v1 fields (``focus.content``, ``candidates[].snippet``) stay as they
were sent, so a v1 run on the same file still renders what v1 saw.

``mirror`` adds one hand-built case per reviewed label with a scored
direction: the two notes swapped and the direction flipped. The production
labels lean ``focus_to_candidate`` because the focus is usually the newer
note; the mirrors give the set as many ``candidate_to_focus`` directions, so a
model that answers one direction regardless fails on the labels.

    python scripts/eval_cases.py attach-notes evals/edges/cases.jsonl \\
        --vault prod=~/projects/lithos/data/prod/lithos/knowledge \\
        --vault staging=~/projects/lithos/data/staging/lithos/knowledge
    python scripts/eval_cases.py mirror evals/edges/cases.jsonl evals/edges/labels.jsonl \
        --exclude evals/edges/mirror-exclude.txt

A mirror only works when the swapped prompt still shows what the label rests
on. A focus never shows a matching passage, so a slot whose evidence was in the
candidate's passage cannot be mirrored; ``--exclude`` lists those slots, one
``case_id/index`` per line (``#`` starts a comment).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lithos.frontmatter_codec import decode
from lithos.lcma.edge_inference import SYMMETRIC_RELATIONS
from lithos.lcma.note_view import view_of

_ID_LINE = re.compile(r"^id: *['\"]?([^'\"\s]+)", re.MULTILINE)
_FLIPPED = {"focus_to_candidate": "candidate_to_focus", "candidate_to_focus": "focus_to_candidate"}
MIRROR_PREFIX = "mirror-"
#: Prompt v1 showed the focus's first characters; a mirrored focus gets the same.
_V1_CHARS = 700


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows),
        encoding="utf-8",
    )


def index_vault(root: Path, wanted: set[str]) -> dict[str, Path]:
    """``{note id: path}`` for the *wanted* ids found under *root*."""
    found: dict[str, Path] = {}
    for path in root.rglob("*.md"):
        try:
            head = path.read_text(encoding="utf-8")[:4000]
        except (OSError, UnicodeDecodeError):
            continue
        match = _ID_LINE.search(head)
        if match and match.group(1) in wanted:
            found.setdefault(match.group(1), path)
    return found


def _note(root: Path, path: Path) -> tuple[dict[str, Any], int]:
    doc = decode(path.read_text(encoding="utf-8"), path.relative_to(root))
    return view_of(doc).to_dict(), doc.metadata.version


def attach_notes(
    cases: list[dict[str, Any]], vaults: list[tuple[str, Path]], snippet_chars: int
) -> tuple[int, list[str]]:
    """Attach note views in place; returns ``(notes attached, missing note ids)``."""
    wanted = {str(c["focus"].get("id")) for c in cases}
    wanted |= {str(cand["node_id"]) for c in cases for cand in c["candidates"]}
    indexes = [(name, root, index_vault(root, wanted)) for name, root in vaults]
    attached = 0
    missing: list[str] = []
    for case in cases:
        focus_id = str(case["focus"].get("id"))
        # A case's notes come from the instance it was exported from: the vault
        # holding its focus note, preferring the one whose text matches the text sent.
        holders = [v for v in indexes if focus_id in v[2]]
        sent = str(case["focus"].get("content", ""))[:200]
        holders.sort(key=lambda v: not v[2][focus_id].read_text(encoding="utf-8").count(sent))
        order = holders + [v for v in indexes if v not in holders]
        versions: dict[str, int] = {}
        for item in [case["focus"], *case["candidates"]]:
            node_id = str(item.get("id") or item.get("node_id"))
            source = next(((r, ix[node_id]) for _, r, ix in order if node_id in ix), None)
            if source is None:
                missing.append(node_id)
                continue
            item["note"], versions[node_id] = _note(*source)
            attached += 1
        case["snippet_chars"] = snippet_chars
        case["note_versions"] = versions
        if holders:
            case["note_vault"] = holders[0][0]
    return attached, missing


def mirror_cases(
    cases: list[dict[str, Any]],
    labels: list[dict[str, Any]],
    today: str,
    exclude: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Cases and labels with the mirrors regenerated (any earlier mirrors dropped).

    *exclude* holds ``case_id/index`` slots not to mirror.
    """
    cases = [c for c in cases if not str(c["case_id"]).startswith(MIRROR_PREFIX)]
    labels = [lab for lab in labels if not str(lab["case_id"]).startswith(MIRROR_PREFIX)]
    by_id = {c["case_id"]: c for c in cases}
    sources = sorted(
        (
            lab
            for lab in labels
            if _mirrorable(lab) and f"{lab['case_id']}/{lab['candidate_index']}" not in exclude
        ),
        key=lambda lab: (lab["case_id"], lab["candidate_index"]),
    )
    new_cases: list[dict[str, Any]] = []
    new_labels: list[dict[str, Any]] = []
    for n, label in enumerate(sources, start=1):
        case = by_id[label["case_id"]]
        index = int(label["candidate_index"])
        mirror_id = f"{MIRROR_PREFIX}{n:02d}"
        new_cases.append(_mirror(case, index, mirror_id))
        new_labels.append(
            {
                "case_id": mirror_id,
                "candidate_index": 1,
                "relation": label["relation"],
                "direction": _FLIPPED[label["direction"]],
                "confidence": label.get("confidence", "high"),
                "reference_rationale_ok": None,
                "labeller": label.get("labeller", ""),
                "labelled_at": today,
                "status": "reviewed",
                "notes": f"Mirror of {case['case_id']}/{index}: notes swapped, direction flipped.",
            }
        )
    return cases + new_cases, labels + new_labels


def read_exclusions(path: Path | None) -> frozenset[str]:
    """``case_id/index`` slots from an exclusion file; comments and blanks ignored."""
    if path is None:
        return frozenset()
    lines = (line.split("#", 1)[0].strip() for line in path.read_text().splitlines())
    return frozenset(line for line in lines if line)


def _mirrorable(label: dict[str, Any]) -> bool:
    return (
        label.get("status", "reviewed") == "reviewed"
        and label["relation"] != "none"
        and label["relation"] not in SYMMETRIC_RELATIONS
        and label.get("direction") in _FLIPPED
    )


def _mirror(case: dict[str, Any], index: int, mirror_id: str) -> dict[str, Any]:
    """*case*'s candidate *index* as the focus, and its focus as the only candidate."""
    focus = case["focus"]
    cand = case["candidates"][index - 1]
    cand_note = cand.get("note")
    new_focus: dict[str, Any] = {
        "id": cand["node_id"],
        "title": cand["title"],
        # What prompt v1 would have sent: the note's first characters.
        "content": (cand_note or {}).get("content", cand.get("snippet", ""))[:_V1_CHARS],
    }
    new_cand: dict[str, Any] = {
        "index": 1,
        "node_id": focus.get("id"),
        "title": focus["title"],
        # The search chunk is unknown in this direction; v1 showed this text of it.
        "snippet": str(focus.get("content", ""))[:_V1_CHARS],
        "similarity": cand.get("similarity", 0.0),
        "namespace": cand.get("namespace", "default"),
    }
    if cand_note is not None:
        new_focus["note"] = cand_note
    if focus.get("note") is not None:
        new_cand["note"] = focus["note"]
    return {
        "case_id": mirror_id,
        "tags": ["positive:mirror", f"mirror-of:{case['case_id']}/{index}"],
        "focus": new_focus,
        "candidates": [new_cand],
        "messages": None,
        "prompt_version": None,
        "reference": None,
        "snippet_chars": case.get("snippet_chars"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    att = sub.add_parser("attach-notes", help="attach prompt-v2 note views from vaults")
    att.add_argument("cases", type=Path)
    att.add_argument("--vault", action="append", required=True, help="NAME=knowledge dir")
    att.add_argument("--snippet-chars", type=int, default=1200)
    mir = sub.add_parser("mirror", help="regenerate the mirrored candidate_to_focus cases")
    mir.add_argument("cases", type=Path)
    mir.add_argument("labels", type=Path)
    mir.add_argument("--exclude", type=Path, help="file of case_id/index slots not to mirror")
    args = parser.parse_args(argv)

    if args.cmd == "attach-notes":
        vaults = []
        for spec in args.vault:
            name, _, root = spec.partition("=")
            vaults.append((name, Path(root).expanduser()))
        cases = _read_jsonl(args.cases)
        attached, missing = attach_notes(cases, vaults, args.snippet_chars)
        _write_jsonl(args.cases, cases)
        print(f"{attached} notes attached across {len(cases)} cases; {len(missing)} missing")
        for node_id in sorted(set(missing)):
            print(f"  missing: {node_id}", file=sys.stderr)
        return 1 if missing else 0

    cases, labels = mirror_cases(
        _read_jsonl(args.cases),
        _read_jsonl(args.labels),
        datetime.now(UTC).date().isoformat(),
        read_exclusions(args.exclude),
    )
    _write_jsonl(args.cases, cases)
    _write_jsonl(args.labels, labels)
    mirrors = sum(1 for c in cases if str(c["case_id"]).startswith(MIRROR_PREFIX))
    print(f"{mirrors} mirrored cases; {len(cases)} cases and {len(labels)} labels written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
