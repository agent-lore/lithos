"""Golden-set cases and human labels (eval suite E2 schema).

``cases.jsonl`` holds what the model sees (exported by ``lithos eval export``
or written by hand for deliberate negatives); ``labels.jsonl`` holds one
human judgement per ``(case, candidate)`` slot. Only ``reviewed`` labels are
ground truth — ``proposed`` rows are a labelling aid and are ignored by the
scorer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from lithos.lcma.edge_inference import RELATION_VOCAB, SYMMETRIC_RELATIONS, NeighbourInfo

LABEL_RELATIONS = RELATION_VOCAB | {"none"}
LABEL_DIRECTIONS = frozenset({"focus_to_candidate", "candidate_to_focus"})
LABEL_CONFIDENCES = frozenset({"low", "medium", "high"})
LABEL_STATUSES = frozenset({"proposed", "reviewed"})

#: Unique key for one judgement slot.
Slot = tuple[str, int]


@dataclass(frozen=True)
class Case:
    case_id: str
    focus: dict[str, Any]
    candidates: list[dict[str, Any]]
    messages: list[dict[str, str]] | None
    prompt_version: int | None
    snippet_chars: int
    reference: dict[str, Any] | None = None
    tags: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def n_candidates(self) -> int:
        return len(self.candidates)

    def neighbours(self) -> list[NeighbourInfo]:
        """Rebuild the prompt inputs so a new prompt version can be rendered."""
        return [
            NeighbourInfo(
                node_id=str(c.get("node_id", f"candidate-{c.get('index', i)}")),
                title=str(c.get("title", "")),
                snippet=str(c.get("snippet", "")),
                similarity=float(c.get("similarity", 0.0)),
                namespace=str(c.get("namespace", "default")),
                entities=frozenset(),
                tags=frozenset(),
            )
            for i, c in enumerate(self.candidates, start=1)
        ]

    def reference_answers(self) -> dict[int, tuple[str, str | None]]:
        """``{candidate_index: (relation, direction)}`` from the reference judgements.

        Candidates without a reference judgement count as ``none``: the parser
        drops ``none`` entries, so "judged none" and "not judged" are the same
        thing in a recorded completion.
        """
        answers: dict[int, tuple[str, str | None]] = {}
        judgements = (self.reference or {}).get("judgements") or []
        for j in judgements:
            relation = str(j["relation"])
            direction = None if relation in SYMMETRIC_RELATIONS else j.get("direction")
            answers[int(j["candidate_index"])] = (relation, direction)
        return answers


@dataclass(frozen=True)
class Label:
    case_id: str
    candidate_index: int
    relation: str
    direction: str | None
    confidence: str
    reference_rationale_ok: bool | None
    labeller: str
    labelled_at: str
    status: str = "reviewed"
    notes: str = ""

    @property
    def slot(self) -> Slot:
        return (self.case_id, self.candidate_index)

    @property
    def directed(self) -> bool:
        return self.relation not in SYMMETRIC_RELATIONS and self.relation != "none"


def case_from_dict(data: dict[str, Any]) -> Case:
    case_id = str(data.get("case_id", "")).strip()
    if not case_id:
        raise ValueError("case missing case_id")
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError(f"case {case_id}: candidates must be a non-empty list")
    focus = data.get("focus")
    if not isinstance(focus, dict) or "title" not in focus or "content" not in focus:
        raise ValueError(f"case {case_id}: focus needs title and content")
    messages = data.get("messages")
    if messages is not None and not (
        isinstance(messages, list)
        and all(isinstance(m, dict) and {"role", "content"} <= set(m) for m in messages)
    ):
        raise ValueError(f"case {case_id}: messages must be role/content objects")
    prompt_version = data.get("prompt_version")
    snippet_chars = int(data.get("snippet_chars") or 700)
    tags = tuple(str(t) for t in data.get("tags", []))
    return Case(
        case_id=case_id,
        focus=focus,
        candidates=candidates,
        messages=messages,
        prompt_version=None if prompt_version is None else int(prompt_version),
        snippet_chars=snippet_chars,
        reference=data.get("reference"),
        tags=tags,
        raw=data,
    )


def label_from_dict(data: dict[str, Any]) -> Label:
    case_id = str(data.get("case_id", "")).strip()
    relation = str(data.get("relation", "")).strip()
    if not case_id:
        raise ValueError("label missing case_id")
    if relation not in LABEL_RELATIONS:
        raise ValueError(f"label {case_id}: relation {relation!r} not in {sorted(LABEL_RELATIONS)}")
    index = data.get("candidate_index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 1:
        raise ValueError(f"label {case_id}: candidate_index must be a positive int")
    direction = data.get("direction")
    directed = relation not in SYMMETRIC_RELATIONS and relation != "none"
    if directed and direction not in LABEL_DIRECTIONS:
        raise ValueError(f"label {case_id}/{index}: {relation} needs a direction")
    if not directed:
        direction = None
    confidence = str(data.get("confidence", "high"))
    if confidence not in LABEL_CONFIDENCES:
        raise ValueError(f"label {case_id}/{index}: confidence must be low|medium|high")
    status = str(data.get("status", "reviewed"))
    if status not in LABEL_STATUSES:
        raise ValueError(f"label {case_id}/{index}: status must be proposed|reviewed")
    rationale_ok = data.get("reference_rationale_ok")
    if rationale_ok is not None and not isinstance(rationale_ok, bool):
        raise ValueError(f"label {case_id}/{index}: reference_rationale_ok must be true/false")
    return Label(
        case_id=case_id,
        candidate_index=index,
        relation=relation,
        direction=direction,
        confidence=confidence,
        reference_rationale_ok=rationale_ok,
        labeller=str(data.get("labeller", "")),
        labelled_at=str(data.get("labelled_at", "")),
        status=status,
        notes=str(data.get("notes", "")),
    )


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def cases_fingerprint(cases: list[Case]) -> str:
    """Digest of what the model is scored on: ids, inputs and reference judgements.

    Two runs are comparable only when this matches — same cases, same
    candidate text, same reference — not merely the same file path or count.
    """
    return _digest(
        [
            {
                "case_id": c.case_id,
                "focus": c.focus,
                "candidates": c.candidates,
                "snippet_chars": c.snippet_chars,
                "reference": (c.reference or {}).get("judgements"),
            }
            for c in sorted(cases, key=lambda c: c.case_id)
        ]
    )


def labels_fingerprint(labels: dict[Slot, Label] | None) -> str | None:
    """Digest of the reviewed labels a run was scored against; ``None`` when unlabelled."""
    if not labels:
        return None
    return _digest([asdict(labels[slot]) for slot in sorted(labels)])


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError(f"{path}:{lineno}: expected an object")
            rows.append(data)
    return rows


def load_cases(path: Path, *, limit: int | None = None) -> list[Case]:
    cases = [case_from_dict(row) for row in _read_jsonl(path)]
    seen: set[str] = set()
    for case in cases:
        if case.case_id in seen:
            raise ValueError(f"{path}: duplicate case_id {case.case_id}")
        seen.add(case.case_id)
    return cases[:limit] if limit is not None else cases


def load_labels(path: Path, *, reviewed_only: bool = True) -> dict[Slot, Label]:
    """Labels keyed by slot; a duplicate slot is an error (one truth per slot)."""
    labels: dict[Slot, Label] = {}
    for row in _read_jsonl(path):
        label = label_from_dict(row)
        if reviewed_only and label.status != "reviewed":
            continue
        if label.slot in labels:
            raise ValueError(f"{path}: duplicate label for {label.slot}")
        labels[label.slot] = label
    return labels


def check_labels_cover_cases(labels: dict[Slot, Label], cases: list[Case]) -> list[str]:
    """Problems that make a label set unusable: unknown case or candidate index."""
    by_id = {c.case_id: c for c in cases}
    problems: list[str] = []
    for case_id, index in labels:
        case = by_id.get(case_id)
        if case is None:
            problems.append(f"label for unknown case {case_id}")
        elif index > case.n_candidates:
            problems.append(f"label {case_id}/{index} beyond {case.n_candidates} candidates")
    return problems
