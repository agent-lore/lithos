"""Tests for LCMA reinforcement — positive feedback signals."""

import pytest
import pytest_asyncio

from lithos.config import LithosConfig
from lithos.knowledge import KnowledgeManager
from lithos.lcma.edges import EdgeStore
from lithos.lcma.reinforcement import reinforce_cited_nodes, reinforce_edges_between
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
