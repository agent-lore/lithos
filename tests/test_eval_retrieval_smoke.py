"""Smoke gate for the retrieval eval: scorer over the committed seed set, no stores.

The seed numbers are hand-computed, so a scorer change shows up here as a
changed value rather than as "it still runs".
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from lithos.cognitive_memory import score_retrieval_eval

SEED = Path("evals/retrieval/seed")
LOG3 = math.log2(3)


def test_seed_scores_as_hand_computed(tmp_path: Path) -> None:
    path, doc, text = score_retrieval_eval(
        cases_paths=[SEED / "cases.jsonl"],
        labels_paths=[SEED / "labels.jsonl"],
        runs_dir=tmp_path,
        k=10,
        floor=0.35,
    )
    assert path.exists() and path.name.endswith("-retrieval.json")
    assert doc["evidence"]["n_cases"] == 3
    assert doc["evidence"]["n_labels"] == 8
    assert doc["evidence"]["n_unlabelled_slots"] == 0
    assert doc["roots"][0]["slices"] == ["seed"]

    overall = doc["metrics"]["overall"]
    # seed-001 ideal DCG = 2 + 1/log2(3); hybrid returns [2, 0, 1]; seed-002 hybrid returns [0, 2]
    hybrid_001 = (2 + 1 / math.log2(4)) / (2 + 1 / LOG3)
    hybrid_002 = (2 / LOG3) / 2
    assert overall["hybrid"]["ndcg_at_k"] == pytest.approx((hybrid_001 + hybrid_002) / 2)
    assert overall["semantic"]["ndcg_at_k"] == pytest.approx(1.0)
    retrieve_001 = (2 / LOG3) / (2 + 1 / LOG3)
    assert overall["retrieve"]["ndcg_at_k"] == pytest.approx((retrieve_001 + hybrid_002) / 2)
    assert overall["hybrid"]["mrr"] == pytest.approx(0.75)
    assert overall["semantic"]["mrr"] == pytest.approx(1.0)
    assert overall["retrieve"]["mrr"] == pytest.approx(0.5)
    for method in ("hybrid", "semantic", "retrieve"):
        assert overall[method]["n_queries"] == 3
        assert overall[method]["n_with_relevant"] == 2  # seed-003 has nothing relevant
        assert overall[method]["precision_at_3"] == pytest.approx(2 / 9)
        assert overall[method]["unlabelled_share"] == 0.0
    assert overall["retrieve"]["latency_ms_median"] == 120.0

    floor = doc["floor"]
    assert floor["n_with_relevant"] == 2
    assert floor["n_without_relevant"] == 1
    assert floor["signals"]["semantic"]["at_floor"] == {
        "floor": 0.35,
        "hit_rate": 1.0,
        "false_supply_rate": 0.0,
    }
    # retrieve's composite tops both relevant queries with the wrong note
    assert floor["signals"]["retrieve"]["at_floor"]["hit_rate"] == 0.0
    assert floor["signals"]["retrieve"]["at_floor"]["false_supply_rate"] == 0.0
    # its vector similarity supplies on the no-match query
    assert floor["signals"]["retrieve_vector"]["at_floor"]["false_supply_rate"] == 1.0

    assert "## Overall" in text and "| semantic |" in text and "retrieve_vector" in text


def test_labels_outside_pool_refuse_to_score(tmp_path: Path) -> None:
    bad = tmp_path / "labels.jsonl"
    bad.write_text(
        '{"case_id": "seed-001", "note_id": "seed-note-5", "grade": "relevant", '
        '"labeller": "t", "labelled_at": "d"}\n'
    )
    with pytest.raises(ValueError, match="labels do not match cases"):
        score_retrieval_eval(
            cases_paths=[SEED / "cases.jsonl"], labels_paths=[bad], runs_dir=tmp_path
        )
