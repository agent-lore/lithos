"""Smoke tests for the offline salience calibration harness (scripts/calibrate_salience.py).

Seeds a small stats.db and exercises the harness end-to-end on it, so the reusable
tooling stays correct even though its real runs are against prod/staging copies.
"""

from __future__ import annotations

import sys
from pathlib import Path

from lithos.config import LithosConfig
from lithos.lcma.stats import StatsStore

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import calibrate_salience as cs


def test_auc_separates_cleanly() -> None:
    assert cs.auc([1.0, 1.0, 1.0], [0.0, 0.0, 0.0]) == 1.0
    assert cs.auc([0.0], [1.0]) == 0.0
    assert cs.auc([1.0, 0.0], [0.0, 1.0]) == 0.5  # interleaved -> uninformative
    assert cs.auc([], [1.0]) == 0.5  # empty group -> uninformative


def test_auc_handles_ties() -> None:
    # All equal -> every pair is a tie -> 0.5.
    assert cs.auc([0.5, 0.5], [0.5, 0.5]) == 0.5


async def _seed(store: StatsStore) -> None:
    # A "hot" node: many retrievals + a citation. A "cold" node: nothing.
    for _ in range(15):
        await store.increment_node_stats(node_id="hot")
    await store.increment_cited("hot")
    await store.update_salience("cold", -0.45)  # 0.05, collapsed
    await store.increment_node_stats(node_id="warm")  # one retrieval


async def test_load_and_evaluate(test_config: LithosConfig) -> None:
    store = StatsStore(test_config)
    await store.open()
    try:
        await _seed(store)
    finally:
        await store.close()

    nodes = cs.load_nodes(str(store.db_path))
    assert len(nodes) == 3
    hot = next(n for n in nodes if n.retrieval_count == 15)
    assert hot.cited_count == 1
    assert hot.days_since_use is not None

    # Floor backfill report lifts the collapsed mass.
    floor_lines = cs.floor_report(nodes, [0.3])
    assert any("0.30" in line for line in floor_lines)

    # Usage evaluation ranks configs and produces bounded scores.
    results = cs.evaluate_usage(nodes, cs.build_configs())
    assert results
    for r in results:
        assert 0.0 <= r.mean <= 1.0
        assert 0.0 <= r.ranking_auc <= 1.0
    # The hot node (retrievals + citation) should out-score the cold node under
    # the top-ranked config — the whole point of the usage signal.
    top = results[0].config
    scored = cs._score_all(nodes, top)
    by_count = {n.retrieval_count: s for n, s in zip(nodes, scored, strict=True)}
    assert by_count[15] > by_count[0]


async def test_main_runs(test_config: LithosConfig, capsys) -> None:
    store = StatsStore(test_config)
    await store.open()
    try:
        await _seed(store)
    finally:
        await store.close()

    rc = cs.main([str(store.db_path), "--top", "3"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Loaded 3 nodes" in out
    assert "Floor backfill" in out
    assert "Usage signal" in out
