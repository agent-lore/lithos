"""Smoke + validity tests for the offline salience calibration harness.

Seeds a small stats.db and exercises the harness on it, and — because the harness's
whole job is to project the operator backfill and rank usage configs — proves that its
floor projection matches the real store predicate and that its ranking metric can
actually tell differently-tuned configs apart.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lithos.config import LithosConfig
from lithos.lcma.salience import recalibration_eligible
from lithos.lcma.stats import StatsStore

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import calibrate_salience as cs


def test_auc_separates_cleanly() -> None:
    assert cs.auc([1.0, 1.0, 1.0], [0.0, 0.0, 0.0]) == 1.0
    assert cs.auc([0.0], [1.0]) == 0.0
    assert cs.auc([1.0, 0.0], [0.0, 1.0]) == 0.5  # interleaved -> uninformative
    assert cs.auc([], [1.0]) == 0.5  # empty group -> uninformative


def test_auc_handles_ties() -> None:
    assert cs.auc([0.5, 0.5], [0.5, 0.5]) == 0.5


def test_parse_ts_handles_z_and_garbage() -> None:
    parsed = cs._parse_ts("2026-01-01T00:00:00Z")
    assert parsed is not None
    assert parsed.tzinfo is not None  # Z normalised to +00:00
    assert cs._parse_ts("2026-01-01 00:00:00") is not None  # sqlite CURRENT_TIMESTAMP shape
    assert cs._parse_ts("not-a-timestamp") is None  # unparseable -> dropped, not raised
    assert cs._parse_ts(None) is None


async def _seed(store: StatsStore) -> None:
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

    floor_lines = cs.floor_report(nodes, [0.3])
    assert any("0.30" in line for line in floor_lines)

    results = cs.evaluate_usage(nodes, cs.build_configs(), None)
    assert results
    # Sparse feedback + no receipts -> honest "none" (descriptive-only) ranking.
    assert results[0].ranked_by == "none"
    for r in results:
        assert 0.0 <= r.mean <= 1.0
        assert r.ranking_auc is None


async def test_floor_projection_matches_store_backfill(test_config: LithosConfig) -> None:
    """The harness's floor projection must lift exactly the rows the store lifts."""
    floor = 0.3
    store = StatsStore(test_config)
    await store.open()
    try:
        await store.update_salience("collapsed", -0.45)  # 0.05 -> eligible
        await store.update_salience("misleading", -0.45)
        await store.increment_misleading("misleading")  # protected
        await store.update_salience("chronic", -0.45)
        for _ in range(6):
            await store.increment_ignored("chronic")  # ignored 6 > 5, > cited 0 -> protected
        await store.update_salience("light", -0.45)
        for _ in range(2):
            await store.increment_ignored("light")  # not chronic -> eligible
        await store.update_salience("high", 0.4)  # 0.9 -> above floor, untouched

        pre = {n.node_id: n for n in cs.load_nodes(str(store.db_path))}
        expected_lift = {
            nid
            for nid, n in pre.items()
            if recalibration_eligible(
                n.salience,
                floor,
                misleading_count=n.misleading_count,
                ignored_count=n.ignored_count,
                cited_count=n.cited_count,
            )
        }
        assert expected_lift == {"collapsed", "light"}

        lifted = await store.recalibrate_salience_floor(floor)
        assert lifted == len(expected_lift)

        actually_lifted = {
            nid
            for nid in pre
            if (row := await store.get_node_stats(nid)) is not None
            and abs(float(row["salience"]) - floor) < 1e-9
            and pre[nid].salience < floor
        }
        assert actually_lifted == expected_lift
    finally:
        await store.close()


async def test_load_receipts_parses_production_shape(test_config: LithosConfig) -> None:
    """final_nodes is a list of objects; the harness must key on `id`, not the whole dict.

    Two receipts for the same node with different reasons/scouts must aggregate under one
    node id rather than fragmenting into two pseudo-identifiers.
    """
    store = StatsStore(test_config)
    await store.open()
    try:
        for i, reason in enumerate(("q1", "q2")):
            await store.insert_receipt(
                receipt_id=f"rcpt_{i}",
                query=reason,
                limit=10,
                namespace_filter=None,
                scouts_fired=["scout_vector"],
                candidates_considered=1,
                final_nodes=[{"id": "node-1", "reasons": [reason], "scouts": ["scout_vector"]}],
                conflicts_surfaced=[],
                surface_conflicts=False,
                temperature=0.5,
                terrace_reached=1,
            )
    finally:
        await store.close()

    receipts = cs.load_receipts(str(store.db_path))
    assert len(receipts) == 2
    for _ts, node_ids in receipts:
        assert node_ids == ["node-1"]  # id extracted, not str(dict)

    split = cs.build_time_split(receipts, 0.5)
    assert split is not None
    # Both objects collapse to the single node id across the past/future boundary.
    assert set(split.past_count) | set(split.future_nodes) == {"node-1"}


def test_build_time_split_is_position_based() -> None:
    """A 0.7 split of 10 receipts puts exactly 7 in the past, not 8 (no boundary leak)."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    receipts = [(base + timedelta(days=i), [f"n{i}"]) for i in range(10)]
    split = cs.build_time_split(receipts, 0.7)
    assert split is not None
    assert len(split.past_count) == 7  # n0..n6
    assert split.future_nodes == frozenset(f"n{i}" for i in range(7, 10))
    # Degenerate splits fall back to None (descriptive-only).
    assert cs.build_time_split(receipts, 0.0) is None
    assert cs.build_time_split(receipts, 1.0) is None
    assert cs.build_time_split(receipts[:1], 0.5) is None


async def test_time_split_deterministic_for_equal_timestamps(test_config: LithosConfig) -> None:
    """Receipts sharing a second-resolution ts must split by rowid (insertion) order.

    Guards the (ts, rowid) ordering contract: without the rowid tie-break the past/future
    partition of same-timestamp receipts would be nondeterministic.
    """
    import aiosqlite

    store = StatsStore(test_config)
    await store.open()
    try:
        for rid, node in (("r_a", "a"), ("r_b", "b")):  # 'a' inserted before 'b'
            await store.insert_receipt(
                receipt_id=rid,
                query="q",
                limit=10,
                namespace_filter=None,
                scouts_fired=["scout_vector"],
                candidates_considered=1,
                final_nodes=[{"id": node, "reasons": [], "scouts": ["scout_vector"]}],
                conflicts_surfaced=[],
                surface_conflicts=False,
                temperature=0.5,
                terrace_reached=1,
            )
        # Force identical timestamps so only rowid order can disambiguate the split.
        async with aiosqlite.connect(store.db_path) as db:
            await db.execute("UPDATE receipts SET ts = ?", ("2026-01-01T00:00:00+00:00",))
            await db.commit()
    finally:
        await store.close()

    receipts = cs.load_receipts(str(store.db_path))
    assert len(receipts) == 2
    split = cs.build_time_split(receipts, 0.5)
    assert split is not None
    # Earlier rowid ('a') -> past; later rowid ('b') -> held-out future. Deterministic.
    assert set(split.past_count) == {"a"}
    assert split.future_nodes == frozenset({"b"})


def test_future_auc_distinguishes_recency_configs() -> None:
    """The ranking metric must reward a better-tuned config, not treat all as equal.

    Future retrievals here are exactly the recently-used nodes; frequency is identical
    across nodes, so only a recency-aware config can separate them.
    """
    split_time = datetime(2026, 1, 20, tzinfo=UTC)
    recent = {f"r{i}": 3 for i in range(20)}
    stale = {f"s{i}": 3 for i in range(20)}
    past_count = {**recent, **stale}
    past_last = {
        **{k: split_time for k in recent},
        **{k: split_time - timedelta(days=90) for k in stale},
    }
    split = cs.TimeSplit(split_time, past_count, past_last, frozenset(recent))
    nodes = [cs.NodeRow("x", 0.5, 0, 0, 0, 0, None)]  # no citations -> future label used

    recency_heavy = cs.UsageConfig(0.0, 1.0, 7.0, 20.0)
    freq_only = cs.UsageConfig(1.0, 0.0, 7.0, 20.0)
    results = cs.evaluate_usage(nodes, [recency_heavy, freq_only], split)

    assert results[0].ranked_by == "future"
    by_cfg = {r.config: r for r in results}
    # Recency-aware config perfectly separates; the frequency-only config cannot.
    assert by_cfg[recency_heavy].future_auc == 1.0
    assert by_cfg[freq_only].future_auc == 0.5
    # And the informative one ranks first.
    assert results[0].config == recency_heavy


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
