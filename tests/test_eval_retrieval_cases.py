"""lithos.lcma.eval.retrieval.cases — loading, coverage checks and fingerprints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lithos.lcma.eval.retrieval.cases import (
    case_from_dict,
    case_to_dict,
    cases_fingerprint,
    check_labels_cover_cases,
    labels_fingerprint,
    load_queries,
    load_retrieval_cases,
    load_retrieval_labels,
    unlabelled_slots,
    write_jsonl,
)

SEED = Path("evals/retrieval/seed")


def test_seed_round_trips_through_dicts() -> None:
    cases = load_retrieval_cases([SEED / "cases.jsonl"])
    assert [c.case_id for c in cases] == ["seed-001", "seed-002", "seed-003"]
    first = cases[0]
    assert first.pooled_ids() == ["seed-note-1", "seed-note-2", "seed-note-3", "seed-note-4"]
    again = case_from_dict(case_to_dict(first), root=first.root)
    assert again == first
    retrieve_top = first.methods["retrieve"].results[0]
    assert retrieve_top.scouts == ("scout_coactivation",)
    assert retrieve_top.vector_similarity == 0.30


def test_duplicate_case_id_across_roots_is_an_error(tmp_path: Path) -> None:
    copy = tmp_path / "cases.jsonl"
    copy.write_text((SEED / "cases.jsonl").read_text())
    with pytest.raises(ValueError, match="appears in both"):
        load_retrieval_cases([SEED / "cases.jsonl", copy])


def test_labels_cover_seed_and_unlabelled_is_empty() -> None:
    cases = load_retrieval_cases([SEED / "cases.jsonl"])
    labels = load_retrieval_labels([SEED / "labels.jsonl"])
    assert len(labels) == 8
    assert check_labels_cover_cases(labels, cases) == []
    assert unlabelled_slots(cases, labels) == []


def test_label_outside_pool_and_unknown_case_are_reported(tmp_path: Path) -> None:
    cases = load_retrieval_cases([SEED / "cases.jsonl"])
    rows = [
        {
            "case_id": "seed-001",
            "note_id": "seed-note-5",
            "grade": "relevant",
            "labeller": "t",
            "labelled_at": "d",
        },
        {
            "case_id": "nope",
            "note_id": "seed-note-1",
            "grade": "not",
            "labeller": "t",
            "labelled_at": "d",
        },
        {
            "case_id": "seed-002",
            "note_id": "seed-note-4",
            "grade": "not",
            "labeller": "t",
            "labelled_at": "d",
            "status": "proposed",
        },
    ]
    path = tmp_path / "labels.jsonl"
    write_jsonl(path, rows)
    labels = load_retrieval_labels([path])
    assert ("seed-002", "seed-note-4") not in labels  # proposed rows are not reviewed
    problems = check_labels_cover_cases(labels, cases)
    assert any("unknown case" in p for p in problems)
    assert any("not in that case's pool" in p for p in problems)
    assert len(unlabelled_slots(cases, labels)) == 8


def test_bad_grade_rejected(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    path.write_text(
        json.dumps(
            {"case_id": "a", "note_id": "b", "grade": "maybe", "labeller": "t", "labelled_at": "d"}
        )
        + "\n"
    )
    with pytest.raises(ValueError, match="bad grade"):
        load_retrieval_labels([path])


def test_fingerprint_tracks_rankings_not_note_text() -> None:
    cases = load_retrieval_cases([SEED / "cases.jsonl"])
    base = cases_fingerprint(cases)
    retexted = [case_from_dict({**case_to_dict(c), "notes": {}}) for c in cases]
    assert cases_fingerprint(retexted) == base
    rescored = case_to_dict(cases[0])
    rescored["methods"]["hybrid"]["results"][0]["score"] = 0.999
    assert cases_fingerprint([case_from_dict(rescored), *cases[1:]]) != base
    labels = load_retrieval_labels([SEED / "labels.jsonl"])
    assert labels_fingerprint(labels) is not None
    assert labels_fingerprint({}) is None


def test_load_queries_requires_fields_and_unique_ids(tmp_path: Path) -> None:
    path = tmp_path / "queries.jsonl"
    write_jsonl(
        path,
        [
            {"query_id": "q1", "query": "x", "caller": "c", "slice": "s", "scope": {"tags": ["a"]}},
            {"query_id": "q2", "query": "y", "caller": "c", "slice": "s"},
        ],
    )
    queries = load_queries(path)
    assert queries[0].tags == ["a"]
    assert queries[0].namespace_filter is None
    assert queries[1].path_prefix is None
    write_jsonl(path, [{"query_id": "q1", "query": "x", "caller": "c", "slice": "s"}] * 2)
    with pytest.raises(ValueError, match="duplicate query_id"):
        load_queries(path)
    write_jsonl(path, [{"query_id": "q1", "query": "", "caller": "c", "slice": "s"}])
    with pytest.raises(ValueError, match="missing 'query'"):
        load_queries(path)
