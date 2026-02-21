"""Tests for graph module - NetworkX knowledge graph."""

import pytest

from exogram.graph import KnowledgeGraph
from exogram.knowledge import KnowledgeManager


class TestGraphBuilding:
    """Tests for building graph from documents."""

    @pytest.mark.asyncio
    async def test_add_document_to_graph(
        self, knowledge_manager: KnowledgeManager, knowledge_graph: KnowledgeGraph
    ):
        """Add document creates node in graph."""
        doc = await knowledge_manager.create(
            title="Graph Node Test",
            content="Simple document without links.",
            agent="agent",
        )

        knowledge_graph.add_document(doc)

        assert knowledge_graph.has_node(doc.id)

    @pytest.mark.asyncio
    async def test_document_with_links_creates_edges(
        self, knowledge_manager: KnowledgeManager, knowledge_graph: KnowledgeGraph
    ):
        """Document with wiki-links creates edges."""
        # Create target document first
        target = await knowledge_manager.create(
            title="Target Document",
            content="This is the target.",
            agent="agent",
        )
        knowledge_graph.add_document(target)

        # Create source with link to target
        source = await knowledge_manager.create(
            title="Source Document",
            content="See [[target-document]] for more info.",
            agent="agent",
        )
        knowledge_graph.add_document(source)

        # Should have edge from source to target
        assert knowledge_graph.has_edge(source.id, target.id)

    @pytest.mark.asyncio
    async def test_link_resolution_by_slug(
        self, knowledge_manager: KnowledgeManager, knowledge_graph: KnowledgeGraph
    ):
        """Links are resolved by matching slugs."""
        # Create document with specific title
        api_doc = await knowledge_manager.create(
            title="API Design Guide",
            content="Guidelines for API design.",
            agent="agent",
        )
        knowledge_graph.add_document(api_doc)

        # Link using slug
        linking_doc = await knowledge_manager.create(
            title="Development Docs",
            content="Follow the [[api-design-guide]] for APIs.",
            agent="agent",
        )
        knowledge_graph.add_document(linking_doc)

        assert knowledge_graph.has_edge(linking_doc.id, api_doc.id)

    @pytest.mark.asyncio
    async def test_unresolved_links_tracked(
        self, knowledge_manager: KnowledgeManager, knowledge_graph: KnowledgeGraph
    ):
        """Links to nonexistent documents are tracked."""
        doc = await knowledge_manager.create(
            title="Broken Links Doc",
            content="See [[nonexistent-doc]] for details.",
            agent="agent",
        )
        knowledge_graph.add_document(doc)

        # Should have placeholder node for unresolved link
        unresolved = knowledge_graph.get_unresolved_links()
        assert "nonexistent-doc" in unresolved or len(unresolved) > 0

    @pytest.mark.asyncio
    async def test_remove_document_from_graph(
        self, knowledge_manager: KnowledgeManager, knowledge_graph: KnowledgeGraph
    ):
        """Remove document removes node and edges."""
        doc = await knowledge_manager.create(
            title="Removable Doc",
            content="Will be removed.",
            agent="agent",
        )
        knowledge_graph.add_document(doc)
        assert knowledge_graph.has_node(doc.id)

        knowledge_graph.remove_document(doc.id)

        assert not knowledge_graph.has_node(doc.id)

    @pytest.mark.asyncio
    async def test_update_document_updates_edges(
        self, knowledge_manager: KnowledgeManager, knowledge_graph: KnowledgeGraph
    ):
        """Updating document updates its edges."""
        target1 = await knowledge_manager.create(
            title="Target One",
            content="First target.",
            agent="agent",
        )
        target2 = await knowledge_manager.create(
            title="Target Two",
            content="Second target.",
            agent="agent",
        )
        knowledge_graph.add_document(target1)
        knowledge_graph.add_document(target2)

        # Create doc linking to target1
        source = await knowledge_manager.create(
            title="Source",
            content="Link to [[target-one]].",
            agent="agent",
        )
        knowledge_graph.add_document(source)
        assert knowledge_graph.has_edge(source.id, target1.id)

        # Update to link to target2 instead
        updated = await knowledge_manager.update(
            id=source.id,
            agent="agent",
            content="Now links to [[target-two]].",
        )
        knowledge_graph.add_document(updated)  # Re-add updates

        assert not knowledge_graph.has_edge(source.id, target1.id)
        assert knowledge_graph.has_edge(source.id, target2.id)


class TestGraphQueries:
    """Tests for querying the knowledge graph."""

    @pytest.mark.asyncio
    async def test_get_outgoing_links(
        self, knowledge_manager: KnowledgeManager, knowledge_graph: KnowledgeGraph
    ):
        """Get documents linked FROM a document."""
        target1 = await knowledge_manager.create(
            title="Linked Doc One",
            content="Target one.",
            agent="agent",
        )
        target2 = await knowledge_manager.create(
            title="Linked Doc Two",
            content="Target two.",
            agent="agent",
        )
        knowledge_graph.add_document(target1)
        knowledge_graph.add_document(target2)

        source = await knowledge_manager.create(
            title="Hub Document",
            content="Links to [[linked-doc-one]] and [[linked-doc-two]].",
            agent="agent",
        )
        knowledge_graph.add_document(source)

        outgoing = knowledge_graph.get_outgoing_links(source.id)

        assert len(outgoing) == 2
        outgoing_ids = [n["id"] for n in outgoing]
        assert target1.id in outgoing_ids
        assert target2.id in outgoing_ids

    @pytest.mark.asyncio
    async def test_get_incoming_links(
        self, knowledge_manager: KnowledgeManager, knowledge_graph: KnowledgeGraph
    ):
        """Get documents linking TO a document (backlinks)."""
        target = await knowledge_manager.create(
            title="Popular Doc",
            content="Many docs link here.",
            agent="agent",
        )
        knowledge_graph.add_document(target)

        source1 = await knowledge_manager.create(
            title="Linker One",
            content="See [[popular-doc]].",
            agent="agent",
        )
        source2 = await knowledge_manager.create(
            title="Linker Two",
            content="Also see [[popular-doc]].",
            agent="agent",
        )
        knowledge_graph.add_document(source1)
        knowledge_graph.add_document(source2)

        incoming = knowledge_graph.get_incoming_links(target.id)

        assert len(incoming) == 2
        incoming_ids = [n["id"] for n in incoming]
        assert source1.id in incoming_ids
        assert source2.id in incoming_ids

    @pytest.mark.asyncio
    async def test_get_neighbors_both_directions(
        self, knowledge_manager: KnowledgeManager, knowledge_graph: KnowledgeGraph
    ):
        """Get all connected documents (both directions)."""
        center = await knowledge_manager.create(
            title="Center Doc",
            content="Links to [[outgoing-doc]].",
            agent="agent",
        )
        outgoing = await knowledge_manager.create(
            title="Outgoing Doc",
            content="Linked from center.",
            agent="agent",
        )
        incoming = await knowledge_manager.create(
            title="Incoming Doc",
            content="Links to [[center-doc]].",
            agent="agent",
        )

        knowledge_graph.add_document(center)
        knowledge_graph.add_document(outgoing)
        knowledge_graph.add_document(incoming)

        neighbors = knowledge_graph.get_neighbors(center.id)

        neighbor_ids = [n["id"] for n in neighbors]
        assert outgoing.id in neighbor_ids
        assert incoming.id in neighbor_ids

    @pytest.mark.asyncio
    async def test_find_path_between_documents(
        self, knowledge_manager: KnowledgeManager, knowledge_graph: KnowledgeGraph
    ):
        """Find path between two documents."""
        # Create chain: A -> B -> C
        doc_a = await knowledge_manager.create(
            title="Doc A",
            content="Links to [[doc-b]].",
            agent="agent",
        )
        doc_b = await knowledge_manager.create(
            title="Doc B",
            content="Links to [[doc-c]].",
            agent="agent",
        )
        doc_c = await knowledge_manager.create(
            title="Doc C",
            content="End of chain.",
            agent="agent",
        )

        knowledge_graph.add_document(doc_a)
        knowledge_graph.add_document(doc_b)
        knowledge_graph.add_document(doc_c)

        path = knowledge_graph.find_path(doc_a.id, doc_c.id)

        assert path is not None
        assert len(path) == 3
        assert path[0] == doc_a.id
        assert path[1] == doc_b.id
        assert path[2] == doc_c.id

    @pytest.mark.asyncio
    async def test_no_path_returns_none(
        self, knowledge_manager: KnowledgeManager, knowledge_graph: KnowledgeGraph
    ):
        """No path between disconnected documents."""
        doc_a = await knowledge_manager.create(
            title="Isolated A",
            content="No links.",
            agent="agent",
        )
        doc_b = await knowledge_manager.create(
            title="Isolated B",
            content="Also no links.",
            agent="agent",
        )

        knowledge_graph.add_document(doc_a)
        knowledge_graph.add_document(doc_b)

        path = knowledge_graph.find_path(doc_a.id, doc_b.id)

        assert path is None


class TestGraphAnalysis:
    """Tests for graph analysis features."""

    @pytest.mark.asyncio
    async def test_find_orphans(
        self, knowledge_manager: KnowledgeManager, knowledge_graph: KnowledgeGraph
    ):
        """Find documents with no links in or out."""
        # Connected docs
        connected1 = await knowledge_manager.create(
            title="Connected One",
            content="Links to [[connected-two]].",
            agent="agent",
        )
        connected2 = await knowledge_manager.create(
            title="Connected Two",
            content="Linked from one.",
            agent="agent",
        )

        # Orphan doc
        orphan = await knowledge_manager.create(
            title="Orphan Doc",
            content="No links at all.",
            agent="agent",
        )

        knowledge_graph.add_document(connected1)
        knowledge_graph.add_document(connected2)
        knowledge_graph.add_document(orphan)

        orphans = knowledge_graph.find_orphans()

        assert orphan.id in orphans
        assert connected1.id not in orphans
        assert connected2.id not in orphans

    @pytest.mark.asyncio
    async def test_get_graph_stats(
        self, knowledge_manager: KnowledgeManager, knowledge_graph: KnowledgeGraph
    ):
        """Get graph statistics."""
        doc1 = await knowledge_manager.create(
            title="Stats Doc One",
            content="Links to [[stats-doc-two]].",
            agent="agent",
        )
        doc2 = await knowledge_manager.create(
            title="Stats Doc Two",
            content="Target.",
            agent="agent",
        )

        knowledge_graph.add_document(doc1)
        knowledge_graph.add_document(doc2)

        stats = knowledge_graph.get_stats()

        assert "nodes" in stats
        assert "edges" in stats
        assert stats["nodes"] >= 2
        assert stats["edges"] >= 1

    @pytest.mark.asyncio
    async def test_most_linked_documents(
        self, knowledge_manager: KnowledgeManager, knowledge_graph: KnowledgeGraph
    ):
        """Find most frequently linked documents."""
        # Create a popular document
        popular = await knowledge_manager.create(
            title="Popular Reference",
            content="Everyone links here.",
            agent="agent",
        )
        knowledge_graph.add_document(popular)

        # Create multiple docs linking to it
        for i in range(5):
            linker = await knowledge_manager.create(
                title=f"Linker {i}",
                content="See [[popular-reference]] for info.",
                agent="agent",
            )
            knowledge_graph.add_document(linker)

        # Create less popular doc
        less_popular = await knowledge_manager.create(
            title="Less Popular",
            content="Fewer links.",
            agent="agent",
        )
        knowledge_graph.add_document(less_popular)

        linker_to_less = await knowledge_manager.create(
            title="Single Linker",
            content="See [[less-popular]].",
            agent="agent",
        )
        knowledge_graph.add_document(linker_to_less)

        most_linked = knowledge_graph.get_most_linked(limit=3)

        # Popular should be first
        assert len(most_linked) >= 1
        assert most_linked[0]["id"] == popular.id
        assert most_linked[0]["incoming_count"] == 5

    @pytest.mark.asyncio
    async def test_clear_graph(
        self, knowledge_manager: KnowledgeManager, knowledge_graph: KnowledgeGraph
    ):
        """Clear removes all nodes and edges."""
        doc = await knowledge_manager.create(
            title="To Clear",
            content="Will be cleared.",
            agent="agent",
        )
        knowledge_graph.add_document(doc)

        assert knowledge_graph.get_stats()["nodes"] >= 1

        knowledge_graph.clear()

        assert knowledge_graph.get_stats()["nodes"] == 0
        assert knowledge_graph.get_stats()["edges"] == 0


class TestGraphPersistence:
    """Tests for graph persistence."""

    @pytest.mark.asyncio
    async def test_rebuild_from_documents(
        self, knowledge_manager: KnowledgeManager, knowledge_graph: KnowledgeGraph
    ):
        """Graph can be rebuilt from documents."""
        # Create documents
        doc1 = await knowledge_manager.create(
            title="Rebuild One",
            content="Links to [[rebuild-two]].",
            agent="agent",
        )
        doc2 = await knowledge_manager.create(
            title="Rebuild Two",
            content="Target.",
            agent="agent",
        )

        # Build graph
        knowledge_graph.add_document(doc1)
        knowledge_graph.add_document(doc2)

        original_stats = knowledge_graph.get_stats()

        # Clear and rebuild
        knowledge_graph.clear()
        assert knowledge_graph.get_stats()["nodes"] == 0

        # Rebuild from stored documents
        docs, _ = await knowledge_manager.list_all()
        for doc in docs:
            full_doc, _ = await knowledge_manager.read(id=doc.id)
            knowledge_graph.add_document(full_doc)

        rebuilt_stats = knowledge_graph.get_stats()

        assert rebuilt_stats["nodes"] == original_stats["nodes"]
        assert rebuilt_stats["edges"] == original_stats["edges"]
