"""Golden-set case/label schema (lithos.lcma.eval.cases)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lithos.lcma.eval.cases import (
    case_from_dict,
    check_labels_cover_cases,
    label_from_dict,
    load_cases,
    load_labels,
)


def _case(**overrides):
    base = {
        "case_id": "c1",
        "focus": {"id": "f", "title": "Focus", "content": "body"},
        "candidates": [
            {
                "index": 1,
                "node_id": "a",
                "title": "A",
                "snippet": "sa",
                "similarity": 0.6,
                "namespace": "default",
            },
            {
                "index": 2,
                "node_id": "b",
                "title": "B",
                "snippet": "sb",
                "similarity": 0.5,
                "namespace": "default",
            },
        ],
        "messages": None,
        "prompt_version": None,
        "snippet_chars": 700,
        "reference": {
            "model": "ref",
            "judgements": [
                {
                    "candidate_index": 1,
                    "relation": "supports",
                    "direction": "candidate_to_focus",
                    "confidence": 0.8,
                    "rationale": "r",
                },
                {
                    "candidate_index": 2,
                    "relation": "contradicts",
                    "direction": "focus_to_candidate",
                    "confidence": 0.7,
                    "rationale": "r",
                },
            ],
        },
    }
    base.update(overrides)
    return base


def _label(**overrides):
    base = {
        "case_id": "c1",
        "candidate_index": 1,
        "relation": "supports",
        "direction": "candidate_to_focus",
        "confidence": "high",
        "reference_rationale_ok": True,
        "labeller": "dave",
        "labelled_at": "2026-10-15",
    }
    base.update(overrides)
    return base


class TestCase:
    def test_neighbours_and_reference_answers(self) -> None:
        case = case_from_dict(_case())
        assert [n.node_id for n in case.neighbours()] == ["a", "b"]
        assert case.n_candidates == 2
        # symmetric relations lose their direction; missing slots are none
        assert case.reference_answers() == {
            1: ("supports", "candidate_to_focus"),
            2: ("contradicts", None),
        }

    def test_tags_and_defaults(self) -> None:
        case = case_from_dict(_case(tags=["negative:unrelated"], snippet_chars=None))
        assert case.tags == ("negative:unrelated",)
        assert case.snippet_chars == 700

    @pytest.mark.parametrize(
        "bad, match",
        [
            ({"case_id": ""}, "case_id"),
            ({"candidates": []}, "candidates"),
            ({"focus": {"title": "x"}}, "focus"),
            ({"messages": [{"role": "user"}]}, "messages"),
        ],
    )
    def test_invalid_cases_rejected(self, bad, match) -> None:
        with pytest.raises(ValueError, match=match):
            case_from_dict(_case(**bad))


class TestLabel:
    def test_round_trip(self) -> None:
        label = label_from_dict(_label())
        assert label.slot == ("c1", 1)
        assert label.directed
        assert label.status == "reviewed"

    def test_symmetric_and_none_drop_direction(self) -> None:
        assert label_from_dict(_label(relation="contradicts")).direction is None
        assert label_from_dict(_label(relation="none", direction=None)).direction is None

    @pytest.mark.parametrize(
        "bad, match",
        [
            ({"relation": "likes"}, "relation"),
            ({"candidate_index": 0}, "candidate_index"),
            ({"candidate_index": True}, "candidate_index"),
            ({"direction": None}, "needs a direction"),
            ({"confidence": "sure"}, "confidence"),
            ({"status": "maybe"}, "status"),
            ({"reference_rationale_ok": "yes"}, "reference_rationale_ok"),
        ],
    )
    def test_invalid_labels_rejected(self, bad, match) -> None:
        with pytest.raises(ValueError, match=match):
            label_from_dict(_label(**bad))


class TestLoaders:
    def test_load_cases_and_labels(self, tmp_path: Path) -> None:
        cases = tmp_path / "cases.jsonl"
        cases.write_text(json.dumps(_case()) + "\n\n" + json.dumps(_case(case_id="c2")) + "\n")
        labels = tmp_path / "labels.jsonl"
        labels.write_text(
            json.dumps(_label())
            + "\n"
            + json.dumps(
                _label(candidate_index=2, relation="none", direction=None, status="proposed")
            )
            + "\n"
        )
        loaded = load_cases(cases)
        assert [c.case_id for c in loaded] == ["c1", "c2"]
        assert [c.case_id for c in load_cases(cases, limit=1)] == ["c1"]
        reviewed = load_labels(labels)
        assert list(reviewed) == [("c1", 1)]
        assert len(load_labels(labels, reviewed_only=False)) == 2
        assert check_labels_cover_cases(reviewed, loaded) == []

    def test_duplicates_and_bad_json(self, tmp_path: Path) -> None:
        cases = tmp_path / "cases.jsonl"
        cases.write_text(json.dumps(_case()) + "\n" + json.dumps(_case()) + "\n")
        with pytest.raises(ValueError, match="duplicate case_id"):
            load_cases(cases)
        labels = tmp_path / "labels.jsonl"
        labels.write_text(json.dumps(_label()) + "\n" + json.dumps(_label()) + "\n")
        with pytest.raises(ValueError, match="duplicate label"):
            load_labels(labels)
        broken = tmp_path / "broken.jsonl"
        broken.write_text("{not json\n")
        with pytest.raises(ValueError, match="invalid JSON"):
            load_cases(broken)
        broken.write_text("[1, 2]\n")
        with pytest.raises(ValueError, match="expected an object"):
            load_cases(broken)

    def test_coverage_problems(self) -> None:
        cases = [case_from_dict(_case())]
        labels = {
            ("zz", 1): label_from_dict(_label(case_id="zz")),
            ("c1", 9): label_from_dict(_label(candidate_index=9)),
        }
        problems = check_labels_cover_cases(labels, cases)
        assert any("unknown case zz" in p for p in problems)
        assert any("c1/9 beyond 2" in p for p in problems)
