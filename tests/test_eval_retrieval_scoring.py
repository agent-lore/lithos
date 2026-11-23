"""lithos.lcma.eval.retrieval.scoring — ranking metrics and the floor sweep."""

from __future__ import annotations

import itertools
import math

import pytest

from lithos.lcma.eval.retrieval.cases import (
    MethodRun,
    Ranked,
    RetrievalCase,
    RetrievalLabel,
)
from lithos.lcma.eval.retrieval.scoring import (
    floor_sweep,
    ndcg_at_k,
    percentile,
    precision_at_k,
    reciprocal_rank,
    score_cases,
    score_method,
)


def _label(case_id: str, note_id: str, grade: str) -> RetrievalLabel:
    return RetrievalLabel(
        case_id=case_id, note_id=note_id, grade=grade, labeller="t", labelled_at="2026-11-23"
    )


def _run(*ids: str, scores: list[float] | None = None, latency: float = 10.0) -> MethodRun:
    scores = scores or [1.0 - i * 0.1 for i in range(len(ids))]
    return MethodRun(
        results=tuple(
            Ranked(id=i, rank=r, score=s)
            for r, (i, s) in enumerate(zip(ids, scores, strict=True), start=1)
        ),
        latency_ms=latency,
    )


def _case(case_id: str, slice_name: str = "s", **methods: MethodRun) -> RetrievalCase:
    return RetrievalCase(
        case_id=case_id,
        query="q",
        caller="c",
        slice=slice_name,
        scope={},
        depth=10,
        methods=methods,
        notes={},
    )


class TestPrimitives:
    def test_ndcg_perfect_and_swapped(self) -> None:
        grades = {"a": 2, "b": 1, "c": 0}
        assert ndcg_at_k(["a", "b", "c"], grades, 10) == pytest.approx(1.0)
        swapped = ndcg_at_k(["b", "a", "c"], grades, 10)
        assert swapped == pytest.approx((1 + 2 / math.log2(3)) / (2 + 1 / math.log2(3)))

    def test_ndcg_undefined_without_a_positive_gain(self) -> None:
        assert ndcg_at_k(["a"], {"a": 0}, 10) is None
        assert ndcg_at_k(["a"], {}, 10) is None

    def test_unlabelled_counts_as_not_relevant(self) -> None:
        assert ndcg_at_k(["x", "a"], {"a": 2}, 10) == pytest.approx(1 / math.log2(3))

    def test_ndcg_truncates_ideal_at_k(self) -> None:
        grades = {"a": 2, "b": 2, "c": 2}
        assert ndcg_at_k(["a"], grades, 1) == pytest.approx(1.0)

    def test_precision_counts_relevant_only(self) -> None:
        grades = {"a": 2, "b": 1, "c": 0}
        assert precision_at_k(["a", "b", "c"], grades, 3) == pytest.approx(1 / 3)
        assert precision_at_k([], grades, 3) == 0.0
        # k is the denominator even when fewer results came back
        assert precision_at_k(["a"], grades, 5) == pytest.approx(1 / 5)

    def test_reciprocal_rank(self) -> None:
        assert reciprocal_rank(["b", "a"], {"a": 2, "b": 1}) == pytest.approx(0.5)
        assert reciprocal_rank(["b"], {"a": 2, "b": 1}) == 0.0  # relevant exists, not returned
        assert reciprocal_rank(["b"], {"b": 1}) is None  # nothing relevant known

    def test_percentile(self) -> None:
        assert percentile([], 0.5) is None
        assert percentile([3.0, 1.0, 2.0], 0.5) == 2.0
        assert percentile([1.0, 2.0, 3.0, 4.0, 10.0], 0.9) == 10.0


class TestScoreMethod:
    def test_means_and_unlabelled_share(self) -> None:
        cases = [
            _case("c1", hybrid=_run("a", "x", "b", latency=10.0)),
            _case("c2", hybrid=_run("y", latency=30.0)),  # no labels at all
        ]
        labels = {("c1", "a"): _label("c1", "a", "relevant"), ("c1", "b"): _label("c1", "b", "not")}
        m = score_method(cases, labels, "hybrid", k=10)
        assert m.n_queries == 2
        assert m.n_with_relevant == 1
        assert m.ndcg_at_k == pytest.approx(1.0)
        assert m.mrr == pytest.approx(1.0)
        assert m.precision_at_3 == pytest.approx((1 / 3 + 0) / 2)
        assert m.unlabelled_share == pytest.approx(2 / 4)  # x and y of four slots
        assert m.latency_ms_median == 10.0 or m.latency_ms_median == 30.0
        assert m.latency_ms_p90 == 30.0

    def test_method_absent_from_a_case_is_skipped(self) -> None:
        cases = [_case("c1", hybrid=_run("a")), _case("c2", semantic=_run("a"))]
        labels = {("c1", "a"): _label("c1", "a", "relevant")}
        assert score_method(cases, labels, "hybrid", k=10).n_queries == 1
        assert score_method(cases, labels, "retrieve", k=10).n_queries == 0

    def test_score_cases_groups_by_slice(self) -> None:
        cases = [
            _case("c1", "dave", hybrid=_run("a"), semantic=_run("b")),
            _case("c2", "robot", hybrid=_run("b"), semantic=_run("a")),
        ]
        labels = {
            ("c1", "a"): _label("c1", "a", "relevant"),
            ("c1", "b"): _label("c1", "b", "not"),
            ("c2", "a"): _label("c2", "a", "relevant"),
            ("c2", "b"): _label("c2", "b", "not"),
        }
        doc = score_cases(cases, labels, k=10)
        assert set(doc["overall"]) == {"hybrid", "semantic"}
        assert set(doc["by_slice"]) == {"dave", "robot"}
        assert doc["by_slice"]["dave"]["hybrid"]["mrr"] == pytest.approx(1.0)
        assert doc["by_slice"]["dave"]["semantic"]["mrr"] == pytest.approx(0.0)
        assert doc["overall"]["hybrid"]["mrr"] == pytest.approx(0.5)


class TestFloorSweep:
    def test_hit_and_false_supply_at_floor(self) -> None:
        cases = [
            # relevant note exists; semantic top is relevant and clears 0.35
            _case("c1", semantic=_run("a", "b", scores=[0.6, 0.2]), retrieve=_run("b", "a")),
            # nothing relevant known; semantic still returns something over the floor
            _case("c2", semantic=_run("b", scores=[0.5]), retrieve=_run("b", scores=[0.3])),
            # no labels at all: ignored by the sweep
            _case("c3", semantic=_run("z", scores=[0.9])),
        ]
        labels = {
            ("c1", "a"): _label("c1", "a", "relevant"),
            ("c1", "b"): _label("c1", "b", "not"),
            ("c2", "b"): _label("c2", "b", "not"),
        }
        sweep = floor_sweep(cases, labels, floor=0.35)
        assert sweep["n_with_relevant"] == 1
        assert sweep["n_without_relevant"] == 1
        sem = sweep["signals"]["semantic"]["at_floor"]
        assert sem["hit_rate"] == 1.0
        assert sem["false_supply_rate"] == 1.0
        ret = sweep["signals"]["retrieve"]["at_floor"]
        assert ret["hit_rate"] == 0.0  # top retrieve result is the wrong note
        assert ret["false_supply_rate"] == 0.0  # 0.3 is under the floor
        assert "retrieve_vector" not in sweep["signals"]  # no vector similarity recorded
        # the sweep is monotone: higher floors never supply more
        rates = sweep["signals"]["semantic"]["false_supply_rate"]
        assert all(a >= b for a, b in itertools.pairwise(rates))
