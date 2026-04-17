"""Tests for LCMA reinforcement — positive and negative feedback signals."""

import pytest
import pytest_asyncio

from lithos.config import LithosConfig
from lithos.knowledge import KnowledgeManager
from lithos.lcma.edges import EdgeStore
from lithos.lcma.reinforcement import (
    penalize_ignored,
    penalize_misleading,
    reinforce_cited_nodes,
    reinforce_edges_between,
    weaken_edges_for_bad_context,
)
from lithos.lcma.stats import StatsStore


@pytest_asyncio.fixture
async def edge_store(test_config: LithosConfig) -> EdgeStore:
    """Create and open an EdgeStore for testing."""
    store = EdgeStore(test_config)
    await store.open()
    return store


@pytest_asyncio.fixture
async def stats_store(test_config: LithosConfig) -> StatsStore:
    """Create and open a StatsStore for testing."""
    store = StatsStore(test_config)
    await store.open()
    return store


async def _create_note(
    km: KnowledgeManager,
    title: str,
    *,
    namespace: str | None = None,
) -> str:
    """Helper: create a note and return its doc ID."""
    result = await km.create(
        title=title,
        content=f"Content for {title}",
        agent="test-agent",
        namespace=namespace,
    )
    assert result.document is not None
    return result.document.id


class TestReinforceCitedNodes:
    """reinforce_cited_nodes: stats increments for each cited node."""

    async def test_increments_cited_count(
        self,
        knowledge_manager: KnowledgeManager,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        nid = await _create_note(knowledge_manager, "Note A")

        await reinforce_cited_nodes([nid], edge_store, stats_store, knowledge_manager)

        stats = await stats_store.get_node_stats(nid)
        assert stats is not None
        assert stats["cited_count"] == 1

    async def test_increments_salience(
        self,
        knowledge_manager: KnowledgeManager,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        nid = await _create_note(knowledge_manager, "Note B")

        await reinforce_cited_nodes([nid], edge_store, stats_store, knowledge_manager)

        stats = await stats_store.get_node_stats(nid)
        assert stats is not None
        # Default salience is 0.5, after +0.02 should be 0.52
        assert stats["salience"] == pytest.approx(0.52)

    async def test_increments_spaced_rep_strength(
        self,
        knowledge_manager: KnowledgeManager,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        nid = await _create_note(knowledge_manager, "Note C")

        await reinforce_cited_nodes([nid], edge_store, stats_store, knowledge_manager)

        stats = await stats_store.get_node_stats(nid)
        assert stats is not None
        assert stats["spaced_rep_strength"] == pytest.approx(0.05)

    async def test_multiple_reinforcements_accumulate(
        self,
        knowledge_manager: KnowledgeManager,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        nid = await _create_note(knowledge_manager, "Note D")

        await reinforce_cited_nodes([nid], edge_store, stats_store, knowledge_manager)
        await reinforce_cited_nodes([nid], edge_store, stats_store, knowledge_manager)

        stats = await stats_store.get_node_stats(nid)
        assert stats is not None
        assert stats["cited_count"] == 2
        assert stats["salience"] == pytest.approx(0.54)
        assert stats["spaced_rep_strength"] == pytest.approx(0.10)

    async def test_multiple_nodes_reinforced(
        self,
        knowledge_manager: KnowledgeManager,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        n1 = await _create_note(knowledge_manager, "Note E1")
        n2 = await _create_note(knowledge_manager, "Note E2")

        await reinforce_cited_nodes([n1, n2], edge_store, stats_store, knowledge_manager)

        for nid in [n1, n2]:
            stats = await stats_store.get_node_stats(nid)
            assert stats is not None
            assert stats["cited_count"] == 1
            assert stats["salience"] == pytest.approx(0.52)


class TestReinforceEdgesBetween:
    """reinforce_edges_between: edge creation/strengthening between cited pairs."""

    async def test_creates_related_to_edge(
        self,
        knowledge_manager: KnowledgeManager,
        edge_store: EdgeStore,
    ) -> None:
        n1 = await _create_note(knowledge_manager, "Note F1")
        n2 = await _create_note(knowledge_manager, "Note F2")

        await reinforce_edges_between([n1, n2], edge_store, knowledge_manager)

        from_id, to_id = sorted([n1, n2])
        edges = await edge_store.list_edges(from_id=from_id, to_id=to_id, edge_type="related_to")
        assert len(edges) == 1
        assert edges[0]["weight"] == pytest.approx(0.5)

    async def test_strengthens_existing_edge(
        self,
        knowledge_manager: KnowledgeManager,
        edge_store: EdgeStore,
    ) -> None:
        n1 = await _create_note(knowledge_manager, "Note G1")
        n2 = await _create_note(knowledge_manager, "Note G2")

        # First call creates the edge at 0.5
        await reinforce_edges_between([n1, n2], edge_store, knowledge_manager)
        # Second call strengthens by +0.03
        await reinforce_edges_between([n1, n2], edge_store, knowledge_manager)

        from_id, to_id = sorted([n1, n2])
        edges = await edge_store.list_edges(from_id=from_id, to_id=to_id, edge_type="related_to")
        assert len(edges) == 1
        assert edges[0]["weight"] == pytest.approx(0.53)

    async def test_canonical_order_from_id_le_to_id(
        self,
        knowledge_manager: KnowledgeManager,
        edge_store: EdgeStore,
    ) -> None:
        """related_to edges are stored with from_id <= to_id lexicographically."""
        n1 = await _create_note(knowledge_manager, "Note H1")
        n2 = await _create_note(knowledge_manager, "Note H2")

        await reinforce_edges_between([n1, n2], edge_store, knowledge_manager)

        all_edges = await edge_store.list_edges(edge_type="related_to")
        assert len(all_edges) == 1
        edge = all_edges[0]
        assert str(edge["from_id"]) <= str(edge["to_id"])

    async def test_skips_cross_namespace_pairs(
        self,
        knowledge_manager: KnowledgeManager,
        edge_store: EdgeStore,
    ) -> None:
        n1 = await _create_note(knowledge_manager, "Note I1", namespace="alpha")
        n2 = await _create_note(knowledge_manager, "Note I2", namespace="beta")

        await reinforce_edges_between([n1, n2], edge_store, knowledge_manager)

        all_edges = await edge_store.list_edges(edge_type="related_to")
        assert len(all_edges) == 0

    async def test_creates_edges_for_same_namespace(
        self,
        knowledge_manager: KnowledgeManager,
        edge_store: EdgeStore,
    ) -> None:
        n1 = await _create_note(knowledge_manager, "Note J1", namespace="shared")
        n2 = await _create_note(knowledge_manager, "Note J2", namespace="shared")

        await reinforce_edges_between([n1, n2], edge_store, knowledge_manager)

        all_edges = await edge_store.list_edges(edge_type="related_to")
        assert len(all_edges) == 1
        assert all_edges[0]["namespace"] == "shared"

    async def test_mixed_namespaces_only_same_namespace_edges(
        self,
        knowledge_manager: KnowledgeManager,
        edge_store: EdgeStore,
    ) -> None:
        """With 3 nodes: 2 in ns-A, 1 in ns-B, only the ns-A pair gets an edge."""
        n1 = await _create_note(knowledge_manager, "Note K1", namespace="ns-a")
        n2 = await _create_note(knowledge_manager, "Note K2", namespace="ns-a")
        n3 = await _create_note(knowledge_manager, "Note K3", namespace="ns-b")

        await reinforce_edges_between([n1, n2, n3], edge_store, knowledge_manager)

        all_edges = await edge_store.list_edges(edge_type="related_to")
        assert len(all_edges) == 1
        assert all_edges[0]["namespace"] == "ns-a"

    async def test_multiple_same_namespace_pairs(
        self,
        knowledge_manager: KnowledgeManager,
        edge_store: EdgeStore,
    ) -> None:
        """Three nodes in same namespace produce 3 edges (C(3,2) = 3)."""
        n1 = await _create_note(knowledge_manager, "Note L1")
        n2 = await _create_note(knowledge_manager, "Note L2")
        n3 = await _create_note(knowledge_manager, "Note L3")

        await reinforce_edges_between([n1, n2, n3], edge_store, knowledge_manager)

        all_edges = await edge_store.list_edges(edge_type="related_to")
        assert len(all_edges) == 3


class TestPenalizeIgnored:
    """penalize_ignored: threshold logic for salience decay."""

    async def test_ignored_count_4_does_not_decay(
        self,
        knowledge_manager: KnowledgeManager,
        stats_store: StatsStore,
    ) -> None:
        """ignored_count=4 (<=5) does NOT trigger salience decay."""
        nid = await _create_note(knowledge_manager, "Note Ign4")

        # Bring ignored_count to 4 (each call increments by 1)
        for _ in range(4):
            await penalize_ignored([nid], stats_store)

        stats = await stats_store.get_node_stats(nid)
        assert stats is not None
        assert stats["ignored_count"] == 4
        # Salience should remain at default 0.5 (no decay applied)
        assert stats["salience"] == pytest.approx(0.5)

    async def test_ignored_count_6_cited_3_does_decay(
        self,
        knowledge_manager: KnowledgeManager,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """ignored_count=6 > 5 and ignored_count=6 > cited_count=3 → decay applied."""
        nid = await _create_note(knowledge_manager, "Note Ign6c3")

        # Set cited_count to 3
        for _ in range(3):
            await stats_store.increment_cited(nid)

        # Bring ignored_count to 6 (threshold > 5 crossed on call 6)
        for _ in range(6):
            await penalize_ignored([nid], stats_store)

        stats = await stats_store.get_node_stats(nid)
        assert stats is not None
        assert stats["ignored_count"] == 6
        # Decay should have been applied once (on the 6th call: ignored=6 > 5 and > 3)
        assert stats["salience"] == pytest.approx(0.5 - 0.02)

    async def test_ignored_count_6_cited_10_no_decay(
        self,
        knowledge_manager: KnowledgeManager,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """ignored_count=6 > 5 but cited_count=10 > ignored_count → NO decay."""
        nid = await _create_note(knowledge_manager, "Note Ign6c10")

        # Set cited_count to 10
        for _ in range(10):
            await stats_store.increment_cited(nid)

        # Bring ignored_count to 6
        for _ in range(6):
            await penalize_ignored([nid], stats_store)

        stats = await stats_store.get_node_stats(nid)
        assert stats is not None
        assert stats["ignored_count"] == 6
        # No decay: ignored_count(6) is NOT > cited_count(10)
        assert stats["salience"] == pytest.approx(0.5)

    async def test_ignored_cumulative_decay(
        self,
        knowledge_manager: KnowledgeManager,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """Repeated calls past threshold apply cumulative salience decay."""
        nid = await _create_note(knowledge_manager, "Note CumDecay")

        # Set cited_count to 3 so condition ignored > cited is eventually true
        for _ in range(3):
            await stats_store.increment_cited(nid)

        # Call 8 times: first 5 only increment, calls 6-8 each decay by 0.02
        for _ in range(8):
            await penalize_ignored([nid], stats_store)

        stats = await stats_store.get_node_stats(nid)
        assert stats is not None
        assert stats["ignored_count"] == 8
        # Calls 6, 7, 8 each apply -0.02 (ignored > 5 and ignored > cited=3)
        expected_salience = 0.5 - (3 * 0.02)
        assert stats["salience"] == pytest.approx(expected_salience)


class TestPenalizeMisleading:
    """penalize_misleading: salience penalty and quarantine threshold."""

    async def test_decrements_salience(
        self,
        knowledge_manager: KnowledgeManager,
        stats_store: StatsStore,
    ) -> None:
        nid = await _create_note(knowledge_manager, "Note Mis1")

        await penalize_misleading([nid], stats_store, knowledge_manager)

        stats = await stats_store.get_node_stats(nid)
        assert stats is not None
        assert stats["misleading_count"] == 1
        assert stats["salience"] == pytest.approx(0.5 - 0.05)

    async def test_quarantine_at_misleading_count_3(
        self,
        knowledge_manager: KnowledgeManager,
        stats_store: StatsStore,
    ) -> None:
        """misleading_count reaching 3 sets status to 'quarantined'."""
        nid = await _create_note(knowledge_manager, "Note Mis3")

        for _ in range(3):
            await penalize_misleading([nid], stats_store, knowledge_manager)

        stats = await stats_store.get_node_stats(nid)
        assert stats is not None
        assert stats["misleading_count"] == 3

        # Verify status was set to quarantined in knowledge manager
        cached = knowledge_manager.get_cached_meta(nid)
        assert cached is not None
        assert cached.status == "quarantined"

    async def test_no_quarantine_at_misleading_count_2(
        self,
        knowledge_manager: KnowledgeManager,
        stats_store: StatsStore,
    ) -> None:
        """misleading_count=2 does NOT quarantine."""
        nid = await _create_note(knowledge_manager, "Note Mis2")

        for _ in range(2):
            await penalize_misleading([nid], stats_store, knowledge_manager)

        cached = knowledge_manager.get_cached_meta(nid)
        assert cached is not None
        assert cached.status != "quarantined"


class TestWeakenEdgesForBadContext:
    """weaken_edges_for_bad_context: weakens all edges touching bad nodes."""

    async def test_weakens_edges_pointing_to_bad_node(
        self,
        knowledge_manager: KnowledgeManager,
        edge_store: EdgeStore,
    ) -> None:
        n1 = await _create_note(knowledge_manager, "Note W1")
        n2 = await _create_note(knowledge_manager, "Note W2")

        # Create an edge between the two nodes
        await reinforce_edges_between([n1, n2], edge_store, knowledge_manager)

        # Weaken edges for n2
        await weaken_edges_for_bad_context([n2], edge_store)

        from_id, to_id = sorted([n1, n2])
        edges = await edge_store.list_edges(from_id=from_id, to_id=to_id, edge_type="related_to")
        assert len(edges) == 1
        # Initial weight 0.5, weakened by -0.05 → 0.45
        assert edges[0]["weight"] == pytest.approx(0.45)

    async def test_shared_edge_weakened_only_once(
        self,
        knowledge_manager: KnowledgeManager,
        edge_store: EdgeStore,
    ) -> None:
        """An edge shared by two bad nodes is weakened exactly once (-0.05)."""
        n1 = await _create_note(knowledge_manager, "Note Bad1")
        n2 = await _create_note(knowledge_manager, "Note Bad2")

        # Create an edge between the two nodes (initial weight 0.5)
        await reinforce_edges_between([n1, n2], edge_store, knowledge_manager)

        # Both endpoints are bad — edge should still only be weakened once
        await weaken_edges_for_bad_context([n1, n2], edge_store)

        from_id, to_id = sorted([n1, n2])
        edges = await edge_store.list_edges(from_id=from_id, to_id=to_id, edge_type="related_to")
        assert len(edges) == 1
        # Initial weight 0.5, weakened by -0.05 exactly once → 0.45
        assert edges[0]["weight"] == pytest.approx(0.45)


class TestQuarantineFiltering:
    """Quarantined nodes must be excluded from list_all and retrieval."""

    async def test_list_all_with_exclude_status(
        self,
        knowledge_manager: KnowledgeManager,
        stats_store: StatsStore,
    ) -> None:
        """list_all with exclude_status=['quarantined'] filters correctly."""
        n1 = await _create_note(knowledge_manager, "Note Active")
        n2 = await _create_note(knowledge_manager, "Note Quarantined")

        # Quarantine n2
        for _ in range(3):
            await penalize_misleading([n2], stats_store, knowledge_manager)

        # list_all without filter returns both
        docs_all, total_all = await knowledge_manager.list_all()
        all_ids = {d.id for d in docs_all}
        assert n1 in all_ids
        assert n2 in all_ids

        # list_all with exclude_status filters out quarantined
        docs_filtered, total_filtered = await knowledge_manager.list_all(
            exclude_status=["quarantined"]
        )
        filtered_ids = {d.id for d in docs_filtered}
        assert n1 in filtered_ids
        assert n2 not in filtered_ids
        assert total_filtered == total_all - 1

    async def test_quarantined_node_excluded_from_retrieval(
        self,
        test_config: LithosConfig,
    ) -> None:
        """Quarantined node is excluded from retrieval results."""
        from typing import Any

        from lithos.server import LithosServer

        server = LithosServer(test_config)
        await server.initialize()
        try:
            ss = server.stats_store
            km = server.knowledge

            async def _call(tool_name: str, **kwargs: Any) -> dict[str, Any]:
                tool = await server.mcp.get_tool(tool_name)
                return await tool.fn(**kwargs)

            # Create two notes via lithos_write so they get indexed
            r1 = await _call(
                "lithos_write",
                title="Good Note",
                content="Unique quarantine test content alpha.",
                agent="agent",
            )
            r2 = await _call(
                "lithos_write",
                title="Bad Note",
                content="Unique quarantine test content alpha.",
                agent="agent",
            )
            good_id = r1["id"]
            bad_id = r2["id"]

            # Quarantine the bad note via 3x penalize_misleading
            for _ in range(3):
                await penalize_misleading([bad_id], ss, km)

            # Retrieve -- the quarantined note should be excluded
            result = await _call("lithos_retrieve", query="quarantine test content alpha", limit=10)
            retrieved_ids = [r["id"] for r in result.get("results", [])]

            assert good_id in retrieved_ids, "Active note should appear in retrieval"
            assert bad_id not in retrieved_ids, "Quarantined note must be excluded from retrieval"
        finally:
            server.stop_file_watcher()
