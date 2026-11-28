"""Retrieval golden set: queries, pooled cases and labels.

A *query* is what a caller asked, with the scope filters it used. A *case*
is that query run through every method at the same depth against one
snapshot, with the union of what they returned (the pool) and a head of
each pooled note's text so a labeller sees what the caller would have. A
*label* grades one query-note pair.

Several case files can be scored together: the public slice lives in this
repo under ``evals/retrieval/``, the personal slices in the private
``lithos-evals`` repo. Case ids must be unique across the files loaded.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

METHODS: tuple[str, ...] = ("hybrid", "semantic", "retrieve")
#: ``relevant``: the caller's next step is better for reading it. ``related``:
#: same topic, the caller would see why it came back but it does not answer
#: the query: partial gain in nDCG, a miss for precision and MRR. ``not``: the rest.
GRADES: Mapping[str, int] = {"relevant": 2, "related": 1, "not": 0}
RELEVANT = GRADES["relevant"]

Slot = tuple[str, str]  # (case_id, note_id)


@dataclass(frozen=True)
class Query:
    query_id: str
    query: str
    caller: str
    slice: str
    scope: dict[str, Any] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)

    @property
    def namespace_filter(self) -> list[str] | None:
        value = self.scope.get("namespace_filter")
        return list(value) if value else None

    @property
    def tags(self) -> list[str] | None:
        value = self.scope.get("tags")
        return list(value) if value else None

    @property
    def path_prefix(self) -> str | None:
        value = self.scope.get("path_prefix")
        return str(value) if value else None


@dataclass(frozen=True)
class Ranked:
    id: str
    rank: int
    score: float
    scouts: tuple[str, ...] = ()
    vector_similarity: float | None = None


@dataclass(frozen=True)
class MethodRun:
    results: tuple[Ranked, ...]
    latency_ms: float

    @property
    def ids(self) -> list[str]:
        return [r.id for r in self.results]


@dataclass(frozen=True)
class RetrievalCase:
    case_id: str
    query: str
    caller: str
    slice: str
    scope: dict[str, Any]
    depth: int
    methods: dict[str, MethodRun]
    notes: dict[str, dict[str, Any]]
    snapshot: dict[str, Any] = field(default_factory=dict)
    root: str = ""

    def pooled_ids(self) -> list[str]:
        """Every note any method returned, in first-seen order over METHODS."""
        seen: list[str] = []
        for method in METHODS:
            run = self.methods.get(method)
            if run is None:
                continue
            seen.extend(r.id for r in run.results if r.id not in seen)
        return seen


@dataclass(frozen=True)
class RetrievalLabel:
    case_id: str
    note_id: str
    grade: str
    labeller: str
    labelled_at: str
    status: str = "reviewed"
    notes: str = ""

    @property
    def gain(self) -> int:
        return GRADES[self.grade]


def query_from_dict(data: dict[str, Any]) -> Query:
    for key in ("query_id", "query", "caller", "slice"):
        if not data.get(key):
            raise ValueError(f"query row missing {key!r}: {data}")
    return Query(
        query_id=str(data["query_id"]),
        query=str(data["query"]),
        caller=str(data["caller"]),
        slice=str(data["slice"]),
        scope=dict(data.get("scope") or {}),
        source=dict(data.get("source") or {}),
    )


def _ranked_from_dict(data: dict[str, Any]) -> Ranked:
    return Ranked(
        id=str(data["id"]),
        rank=int(data["rank"]),
        score=float(data["score"]),
        scouts=tuple(data.get("scouts") or ()),
        vector_similarity=(
            float(data["vector_similarity"]) if data.get("vector_similarity") is not None else None
        ),
    )


def case_from_dict(data: dict[str, Any], *, root: str = "") -> RetrievalCase:
    for key in ("case_id", "query", "caller", "slice", "methods", "notes"):
        if key not in data:
            raise ValueError(f"case row missing {key!r}")
    methods = {
        name: MethodRun(
            results=tuple(_ranked_from_dict(r) for r in run["results"]),
            latency_ms=float(run["latency_ms"]),
        )
        for name, run in data["methods"].items()
    }
    unknown = set(methods) - set(METHODS)
    if unknown:
        raise ValueError(f"case {data['case_id']}: unknown methods {sorted(unknown)}")
    return RetrievalCase(
        case_id=str(data["case_id"]),
        query=str(data["query"]),
        caller=str(data["caller"]),
        slice=str(data["slice"]),
        scope=dict(data.get("scope") or {}),
        depth=int(data.get("depth") or 10),
        methods=methods,
        notes=dict(data["notes"]),
        snapshot=dict(data.get("snapshot") or {}),
        root=root,
    )


def case_to_dict(case: RetrievalCase) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "query": case.query,
        "caller": case.caller,
        "slice": case.slice,
        "scope": case.scope,
        "depth": case.depth,
        "methods": {
            name: {
                "results": [
                    {k: v for k, v in asdict(r).items() if k != "scouts" or v} for r in run.results
                ],
                "latency_ms": round(run.latency_ms, 3),
            }
            for name, run in case.methods.items()
        },
        "notes": case.notes,
        "snapshot": case.snapshot,
    }


def label_from_dict(data: dict[str, Any]) -> RetrievalLabel:
    grade = str(data.get("grade", ""))
    if grade not in GRADES:
        raise ValueError(f"label {data.get('case_id')}/{data.get('note_id')}: bad grade {grade!r}")
    for key in ("case_id", "note_id", "labeller", "labelled_at"):
        if not data.get(key):
            raise ValueError(f"label row missing {key!r}: {data}")
    return RetrievalLabel(
        case_id=str(data["case_id"]),
        note_id=str(data["note_id"]),
        grade=grade,
        labeller=str(data["labeller"]),
        labelled_at=str(data["labelled_at"]),
        status=str(data.get("status") or "reviewed"),
        notes=str(data.get("notes") or ""),
    )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{lineno}: expected an object")
        rows.append(row)
    return rows


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def load_queries(path: Path) -> list[Query]:
    queries = [query_from_dict(row) for row in read_jsonl(path)]
    seen: set[str] = set()
    for q in queries:
        if q.query_id in seen:
            raise ValueError(f"{path}: duplicate query_id {q.query_id!r}")
        seen.add(q.query_id)
    return queries


def load_retrieval_cases(paths: Sequence[Path]) -> list[RetrievalCase]:
    """Cases from every file, in file order; a case id seen twice is an error."""
    cases: list[RetrievalCase] = []
    origin: dict[str, Path] = {}
    for path in paths:
        for row in read_jsonl(path):
            case = case_from_dict(row, root=str(path))
            if case.case_id in origin:
                raise ValueError(
                    f"case {case.case_id!r} appears in both {origin[case.case_id]} and {path}"
                )
            origin[case.case_id] = path
            cases.append(case)
    return cases


def load_retrieval_labels(
    paths: Sequence[Path], *, reviewed_only: bool = True
) -> dict[Slot, RetrievalLabel]:
    labels: dict[Slot, RetrievalLabel] = {}
    for path in paths:
        for row in read_jsonl(path):
            label = label_from_dict(row)
            if reviewed_only and label.status != "reviewed":
                continue
            slot = (label.case_id, label.note_id)
            if slot in labels:
                raise ValueError(f"{path}: slot {slot} labelled twice")
            labels[slot] = label
    return labels


def check_labels_cover_cases(
    labels: Mapping[Slot, RetrievalLabel], cases: Sequence[RetrievalCase]
) -> list[str]:
    """Labels that point at no loaded case, or at a note outside that case's pool."""
    pools = {c.case_id: set(c.pooled_ids()) for c in cases}
    problems: list[str] = []
    for case_id, note_id in sorted(labels):
        if case_id not in pools:
            problems.append(f"label for unknown case {case_id!r}")
        elif note_id not in pools[case_id]:
            problems.append(f"label {case_id}/{note_id} is not in that case's pool")
    return problems


def unlabelled_slots(
    cases: Sequence[RetrievalCase], labels: Mapping[Slot, RetrievalLabel]
) -> list[Slot]:
    """Pooled pairs with no label yet: what an incremental labelling pass must cover."""
    return [
        (case.case_id, note_id)
        for case in cases
        for note_id in case.pooled_ids()
        if (case.case_id, note_id) not in labels
    ]


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def cases_fingerprint(cases: Sequence[RetrievalCase]) -> str:
    """Digest of what is scored: each case's query, scope and every method's ranked ids and scores."""
    return _digest(
        [
            {
                "case_id": c.case_id,
                "query": c.query,
                "scope": c.scope,
                "depth": c.depth,
                "methods": {
                    name: [[r.id, round(r.score, 6)] for r in run.results]
                    for name, run in sorted(c.methods.items())
                },
            }
            for c in sorted(cases, key=lambda c: c.case_id)
        ]
    )


def labels_fingerprint(labels: Mapping[Slot, RetrievalLabel] | None) -> str | None:
    if not labels:
        return None
    return _digest([asdict(labels[slot]) for slot in sorted(labels)])
