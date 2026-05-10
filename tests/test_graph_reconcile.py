"""Tests for KnowledgeGraph._plan_reconcile_to / _apply_reconcile (#250).

ADR-0001 step 2: the wiki-link graph exposes a private plan/apply pair that
KnowledgeManager dispatches through. These tests cover the round-trip plus
KnowledgeManager.apply_reconcile integration; the broader behavioural
coverage (cache_missing/unreadable, node/edge drift, stale links, crash
safety) lives in test_reconcile.py and is unaffected by the migration.
"""

from __future__ import annotations

import pytest

from lithos.config import LithosConfig
from lithos.graph import (
    GraphReconcileAction,
    GraphReconcilePlan,
    GraphReconcileResult,
    KnowledgeGraph,
)
from lithos.knowledge import KnowledgeManager


async def _create_doc(knowledge: KnowledgeManager, title: str, content: str) -> str:
    result = await knowledge.create(title=title, content=content, agent="test")
    assert result.status == "created"
    assert result.document is not None
    return result.document.id


# ---------------------------------------------------------------------------
# KnowledgeGraph._plan_reconcile_to
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_reports_cache_missing_when_no_cache_exists(
    test_config: LithosConfig, knowledge_manager: KnowledgeManager
) -> None:
    """Empty filesystem → plan asks for a full rebuild with reason=cache_missing."""
    await _create_doc(knowledge_manager, "Alpha", "Body alpha.")
    graph = KnowledgeGraph(test_config)
    assert not graph.graph_cache_path.exists()

    corpus = await knowledge_manager._scan_corpus()
    plan = graph._plan_reconcile_to(corpus)

    assert isinstance(plan, GraphReconcilePlan)
    assert plan.scanned == 1
    assert plan.needs_rebuild
    assert any(a.reason == "cache_missing" for a in plan.actions)


@pytest.mark.asyncio
async def test_plan_is_noop_when_cache_matches_corpus(
    test_config: LithosConfig, knowledge_manager: KnowledgeManager
) -> None:
    """Build a cache then re-plan — no drift → is_noop."""
    await _create_doc(knowledge_manager, "Alpha", "Body alpha.")
    await _create_doc(knowledge_manager, "Beta", "Body beta links to [[alpha]].")

    graph = KnowledgeGraph(test_config)
    corpus = await knowledge_manager._scan_corpus()

    # First pass builds and saves the cache.
    first = graph._plan_reconcile_to(corpus)
    assert first.needs_rebuild
    graph._apply_reconcile(first)

    # Second pass on a fresh KG instance reads the cache and reports noop.
    fresh_graph = KnowledgeGraph(test_config)
    second = fresh_graph._plan_reconcile_to(corpus)
    assert second.is_noop
    assert not second.needs_rebuild
    assert second.actions == ()


@pytest.mark.asyncio
async def test_plan_reports_stale_links_when_cache_is_consistent(
    test_config: LithosConfig, knowledge_manager: KnowledgeManager
) -> None:
    """Consistent cache + dangling [[...]] target → stale_link action surfaces."""
    doc_id = await _create_doc(knowledge_manager, "Source", "Points to [[NoSuchTarget]].")

    graph = KnowledgeGraph(test_config)
    corpus = await knowledge_manager._scan_corpus()
    graph._apply_reconcile(graph._plan_reconcile_to(corpus))

    # Replan from a fresh instance: cache loads, no rebuild, stale link reported.
    fresh_graph = KnowledgeGraph(test_config)
    plan = fresh_graph._plan_reconcile_to(corpus)
    assert not plan.needs_rebuild
    stale = [a for a in plan.actions if a.action == "stale_link"]
    assert len(stale) == 1
    assert stale[0].source_id == doc_id
    assert stale[0].link_target == "NoSuchTarget"
    assert stale[0].reason == "target_slug_not_found"


@pytest.mark.asyncio
async def test_plan_reports_node_set_mismatch_after_doc_added(
    test_config: LithosConfig, knowledge_manager: KnowledgeManager
) -> None:
    """Adding a doc after the cache was built → node_set_mismatch on next plan."""
    await _create_doc(knowledge_manager, "Alpha", "Body alpha.")

    graph = KnowledgeGraph(test_config)
    corpus_v1 = await knowledge_manager._scan_corpus()
    graph._apply_reconcile(graph._plan_reconcile_to(corpus_v1))

    # Add a second doc — corpus moves but the cache does not.
    await _create_doc(knowledge_manager, "Beta", "Body beta.")
    corpus_v2 = await knowledge_manager._scan_corpus()

    fresh_graph = KnowledgeGraph(test_config)
    plan = fresh_graph._plan_reconcile_to(corpus_v2)
    assert plan.needs_rebuild
    rebuilds = [a for a in plan.actions if a.action == "full_rebuild"]
    assert any(a.reason == "node_set_mismatch" for a in rebuilds)
    nsm = next(a for a in rebuilds if a.reason == "node_set_mismatch")
    assert nsm.corpus_count == 2
    assert nsm.cached_count == 1


# ---------------------------------------------------------------------------
# KnowledgeGraph._apply_reconcile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_round_trip_writes_cache_and_is_idempotent(
    test_config: LithosConfig, knowledge_manager: KnowledgeManager
) -> None:
    """plan → apply round trip: cache file is created and a re-plan is noop."""
    await _create_doc(knowledge_manager, "Alpha", "Body alpha [[beta]].")
    await _create_doc(knowledge_manager, "Beta", "Body beta.")

    graph = KnowledgeGraph(test_config)
    corpus = await knowledge_manager._scan_corpus()

    plan = graph._plan_reconcile_to(corpus)
    result = graph._apply_reconcile(plan)

    assert isinstance(result, GraphReconcileResult)
    assert result.repaired == 1
    assert result.failed == ()
    assert graph.graph_cache_path.exists()

    # Re-planning from a fresh KG must see no drift.
    fresh = KnowledgeGraph(test_config)
    assert fresh._plan_reconcile_to(corpus).is_noop


@pytest.mark.asyncio
async def test_apply_is_noop_when_plan_only_has_stale_links(
    test_config: LithosConfig, knowledge_manager: KnowledgeManager
) -> None:
    """Stale-link actions are report-only — apply does no work, repaired=0."""
    await _create_doc(knowledge_manager, "Source", "Links to [[NoSuchTarget]].")

    graph = KnowledgeGraph(test_config)
    corpus = await knowledge_manager._scan_corpus()
    graph._apply_reconcile(graph._plan_reconcile_to(corpus))

    fresh_graph = KnowledgeGraph(test_config)
    plan = fresh_graph._plan_reconcile_to(corpus)
    assert all(a.action == "stale_link" for a in plan.actions)

    result = fresh_graph._apply_reconcile(plan)
    assert result.repaired == 0
    assert result.failed == ()
    # Actions are echoed back unchanged for the caller's report.
    assert result.actions == plan.actions


@pytest.mark.asyncio
async def test_apply_records_failure_when_save_cache_raises(
    test_config: LithosConfig,
    knowledge_manager: KnowledgeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """save_cache exception is captured in result.failed without raising."""
    await _create_doc(knowledge_manager, "Alpha", "Body alpha.")

    graph = KnowledgeGraph(test_config)
    corpus = await knowledge_manager._scan_corpus()
    plan = graph._plan_reconcile_to(corpus)

    def boom(self: KnowledgeGraph) -> None:
        raise RuntimeError("simulated cache write failure")

    monkeypatch.setattr(KnowledgeGraph, "save_cache", boom)

    result = graph._apply_reconcile(plan)
    assert result.repaired == 0
    assert len(result.failed) == 1
    assert "simulated cache write failure" in result.failed[0].detail


# ---------------------------------------------------------------------------
# KnowledgeManager dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_km_plan_reconcile_populates_graph_slice(
    test_config: LithosConfig, knowledge_manager: KnowledgeManager
) -> None:
    """KM.plan_reconcile(graph=...) puts a GraphReconcilePlan on the result."""
    await _create_doc(knowledge_manager, "Alpha", "Body alpha.")
    graph = KnowledgeGraph(test_config)

    plan = await knowledge_manager.plan_reconcile(graph=graph)

    assert plan.search is None
    assert plan.graph is not None
    assert isinstance(plan.graph, GraphReconcilePlan)
    assert plan.graph.scanned == 1
    assert plan.graph.needs_rebuild


@pytest.mark.asyncio
async def test_km_apply_reconcile_dispatches_graph(
    test_config: LithosConfig, knowledge_manager: KnowledgeManager
) -> None:
    """KM.apply_reconcile(graph=...) rebuilds the graph and returns its result."""
    await _create_doc(knowledge_manager, "Alpha", "Body alpha.")
    await _create_doc(knowledge_manager, "Beta", "Links to [[alpha]].")
    graph = KnowledgeGraph(test_config)

    plan = await knowledge_manager.plan_reconcile(graph=graph)
    result = await knowledge_manager.apply_reconcile(plan, graph=graph)

    assert result.search is None
    assert result.graph is not None
    assert result.graph.repaired == 1
    assert result.graph.failed == ()
    assert graph.graph_cache_path.exists()


@pytest.mark.asyncio
async def test_km_skips_graph_when_engine_not_passed(
    test_config: LithosConfig, knowledge_manager: KnowledgeManager
) -> None:
    """Without a graph engine the slice is left None — no implicit construction."""
    await _create_doc(knowledge_manager, "Alpha", "Body alpha.")

    plan = await knowledge_manager.plan_reconcile()

    assert plan.search is None
    assert plan.graph is None

    result = await knowledge_manager.apply_reconcile(plan)
    assert result.search is None
    assert result.graph is None


def test_graph_reconcile_action_carries_optional_metadata() -> None:
    """Direct dataclass test — defaults match the dict-shape contract."""
    bare = GraphReconcileAction(target="graph_cache", action="full_rebuild", reason="cache_missing")
    assert bare.source_id is None
    assert bare.corpus_count is None

    enriched = GraphReconcileAction(
        target="wiki_link",
        action="stale_link",
        reason="target_slug_not_found",
        source_id="abc",
        source_title="Source",
        link_target="missing",
    )
    assert enriched.source_id == "abc"
    assert enriched.link_target == "missing"
