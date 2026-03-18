"""Tests for search module - Tantivy and ChromaDB search."""

import pytest

from lithos.errors import IndexingError, SearchBackendError
from lithos.knowledge import KnowledgeManager
from lithos.search import (
    SearchEngine,
    chunk_text,
    reciprocal_rank_fusion,
)


class TestTextChunking:
    """Tests for text chunking algorithm."""

    def test_short_text_single_chunk(self):
        """Short text returns single chunk."""
        text = "This is a short paragraph."
        chunks = chunk_text(text, chunk_size=500)

        assert len(chunks) == 1
        assert chunks[0] == text

    def test_paragraph_boundary_chunking(self):
        """Chunks split at paragraph boundaries."""
        text = """First paragraph with some content.

Second paragraph with more content.

Third paragraph to complete the text."""

        chunks = chunk_text(text, chunk_size=50, chunk_max=100)

        # Should split into multiple chunks at paragraph boundaries
        assert len(chunks) >= 2
        # Each chunk should be a complete paragraph or set of paragraphs
        for chunk in chunks:
            assert not chunk.startswith("\n")
            assert not chunk.endswith("\n\n")

    def test_long_paragraph_sentence_split(self):
        """Very long paragraphs split at sentence boundaries."""
        long_para = "This is sentence one. " * 50  # ~1100 chars
        chunks = chunk_text(long_para, chunk_size=200, chunk_max=400)

        assert len(chunks) > 1
        # Each chunk should end with sentence boundary or be last chunk
        for chunk in chunks[:-1]:
            assert chunk.rstrip().endswith(".") or len(chunk) <= 400

    def test_empty_text(self):
        """Empty text returns empty list."""
        chunks = chunk_text("")
        assert chunks == []

    def test_whitespace_only(self):
        """Whitespace-only text returns empty list."""
        chunks = chunk_text("\n\n   ")
        assert chunks == []

    def test_chunk_size_respected(self):
        """Chunks don't exceed max size (approximately)."""
        text = "Word " * 500  # ~2500 chars
        chunks = chunk_text(text, chunk_size=200, chunk_max=300)

        for chunk in chunks:
            # Allow some overflow for sentence completion
            assert len(chunk) <= 400


class TestTantivyIndex:
    """Tests for Tantivy full-text search."""

    @pytest.mark.asyncio
    async def test_index_and_search(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """Index document and find it via search."""
        doc = (
            await knowledge_manager.create(
                title="Python Tutorial",
                content="Learn Python programming with examples and exercises.",
                agent="agent",
                tags=["python", "tutorial"],
            )
        ).document
        search_engine.index_document(doc)

        results = search_engine.full_text_search("Python programming")

        assert len(results) >= 1
        assert any(r.id == doc.id for r in results)

    @pytest.mark.asyncio
    async def test_search_by_title(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """Search matches document titles."""
        doc = (
            await knowledge_manager.create(
                title="Kubernetes Deployment Guide",
                content="Steps to deploy applications.",
                agent="agent",
            )
        ).document
        search_engine.index_document(doc)

        results = search_engine.full_text_search("Kubernetes")

        assert len(results) >= 1
        assert results[0].title == "Kubernetes Deployment Guide"

    @pytest.mark.asyncio
    async def test_search_by_content(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """Search matches document content."""
        doc = (
            await knowledge_manager.create(
                title="Generic Title",
                content="This document discusses microservices architecture patterns.",
                agent="agent",
            )
        ).document
        search_engine.index_document(doc)

        results = search_engine.full_text_search("microservices architecture")

        assert len(results) >= 1
        assert any(r.id == doc.id for r in results)

    @pytest.mark.asyncio
    async def test_search_with_tag_filter(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """Filter search results by tags."""
        doc1 = (
            await knowledge_manager.create(
                title="Python Web Framework",
                content="Building web apps with Python.",
                agent="agent",
                tags=["python", "web"],
            )
        ).document
        doc2 = (
            await knowledge_manager.create(
                title="Python Data Science",
                content="Data analysis with Python.",
                agent="agent",
                tags=["python", "data"],
            )
        ).document
        search_engine.index_document(doc1)
        search_engine.index_document(doc2)

        # Search with tag filter
        results = search_engine.full_text_search("Python", tags=["web"])

        assert len(results) == 1
        assert results[0].id == doc1.id

    @pytest.mark.asyncio
    async def test_search_no_results(self, search_engine: SearchEngine):
        """Search with no matches returns empty list."""
        results = search_engine.full_text_search("xyznonexistentterm123")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_result_has_snippet(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """Search results include relevant snippets."""
        doc = (
            await knowledge_manager.create(
                title="API Documentation",
                content="The REST API supports GET, POST, PUT, and DELETE methods for resource manipulation.",
                agent="agent",
            )
        ).document
        search_engine.index_document(doc)

        results = search_engine.full_text_search("REST API")

        assert len(results) >= 1
        assert "REST" in results[0].snippet or "API" in results[0].snippet

    @pytest.mark.asyncio
    async def test_document_update_in_index(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """Updated document is re-indexed correctly."""
        doc = (
            await knowledge_manager.create(
                title="Original Title",
                content="Original content about databases.",
                agent="agent",
            )
        ).document
        search_engine.index_document(doc)

        # Update document
        updated = (
            await knowledge_manager.update(
                id=doc.id,
                agent="agent",
                content="Updated content about caching strategies.",
            )
        ).document
        search_engine.index_document(updated)

        # Old content should not match
        old_results = search_engine.full_text_search("databases")
        assert not any(r.id == doc.id for r in old_results)

        # New content should match
        new_results = search_engine.full_text_search("caching strategies")
        assert any(r.id == doc.id for r in new_results)

    @pytest.mark.asyncio
    async def test_document_removal_from_index(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """Removed document no longer appears in search."""
        doc = (
            await knowledge_manager.create(
                title="Temporary Doc",
                content="This will be removed from the index.",
                agent="agent",
            )
        ).document
        search_engine.index_document(doc)

        # Verify it's searchable
        results = search_engine.full_text_search("Temporary")
        assert any(r.id == doc.id for r in results)

        # Remove from index
        search_engine.remove_document(doc.id)

        # Should no longer appear
        results = search_engine.full_text_search("Temporary")
        assert not any(r.id == doc.id for r in results)


class TestChromaIndex:
    """Tests for ChromaDB semantic search."""

    @pytest.mark.asyncio
    async def test_semantic_search_similar_meaning(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """Semantic search finds documents with similar meaning."""
        doc = (
            await knowledge_manager.create(
                title="Error Handling Best Practices",
                content="Always catch exceptions and provide meaningful error messages to users.",
                agent="agent",
            )
        ).document
        search_engine.index_document(doc)

        # Search with semantically similar but different words
        results = search_engine.semantic_search("how to handle failures gracefully")

        assert len(results) >= 1
        # Should find the error handling doc even though "failures" != "exceptions"

    @pytest.mark.asyncio
    async def test_semantic_search_threshold(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """Semantic search respects similarity threshold."""
        doc = (
            await knowledge_manager.create(
                title="Machine Learning Basics",
                content="Neural networks learn patterns from training data.",
                agent="agent",
            )
        ).document
        search_engine.index_document(doc)

        # High threshold should filter out weak matches
        results = search_engine.semantic_search(
            "cooking recipes for dinner",  # Unrelated query
            threshold=0.8,
        )

        # Should not match unrelated content with high threshold
        assert not any(r.id == doc.id for r in results) or results[0].similarity < 0.8

    @pytest.mark.asyncio
    async def test_semantic_search_deduplication(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """Semantic search deduplicates results by document."""
        # Create document with content that will create multiple chunks
        long_content = "\n\n".join(
            [
                "Paragraph about Python programming and best practices.",
                "Another section discussing Python code quality.",
                "More content related to Python development workflows.",
                "Final thoughts on Python ecosystem and tools.",
            ]
        )

        doc = (
            await knowledge_manager.create(
                title="Python Development",
                content=long_content,
                agent="agent",
            )
        ).document
        search_engine.index_document(doc)

        results = search_engine.semantic_search("Python programming", limit=10)

        # Should only return the document once, not once per chunk
        doc_ids = [r.id for r in results]
        assert doc_ids.count(doc.id) <= 1

    @pytest.mark.asyncio
    async def test_semantic_search_returns_similarity_score(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """Semantic search results include similarity scores."""
        doc = (
            await knowledge_manager.create(
                title="Database Optimization",
                content="Index your database tables for faster queries.",
                agent="agent",
            )
        ).document
        search_engine.index_document(doc)

        results = search_engine.semantic_search("database performance tuning")

        assert len(results) >= 1
        assert 0 <= results[0].similarity <= 1


class TestSearchEngineIntegration:
    """Integration tests for combined search functionality."""

    @pytest.mark.asyncio
    async def test_index_multiple_documents(
        self,
        knowledge_manager: KnowledgeManager,
        search_engine: SearchEngine,
        sample_documents: list,
    ):
        """Index and search across multiple documents."""
        created_docs = []
        for doc_data in sample_documents:
            doc = (
                await knowledge_manager.create(
                    title=doc_data["title"],
                    content=doc_data["content"],
                    agent="test-agent",
                    tags=doc_data["tags"],
                )
            ).document
            search_engine.index_document(doc)
            created_docs.append(doc)

        # Full-text search
        ft_results = search_engine.full_text_search("Python")
        assert len(ft_results) >= 2  # Should find Python-related docs

        # Semantic search
        sem_results = search_engine.semantic_search("how to write good code")
        assert len(sem_results) >= 1

    @pytest.mark.asyncio
    async def test_search_ranking(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """More relevant documents rank higher."""
        # Create docs with varying relevance to "Python testing"
        highly_relevant = (
            await knowledge_manager.create(
                title="Python Testing with Pytest",
                content="Comprehensive guide to testing Python applications using pytest framework.",
                agent="agent",
            )
        ).document
        somewhat_relevant = (
            await knowledge_manager.create(
                title="Python Basics",
                content="Introduction to Python programming. Testing is mentioned briefly.",
                agent="agent",
            )
        ).document
        not_relevant = (
            await knowledge_manager.create(
                title="JavaScript Guide",
                content="Learn JavaScript for web development.",
                agent="agent",
            )
        ).document

        search_engine.index_document(highly_relevant)
        search_engine.index_document(somewhat_relevant)
        search_engine.index_document(not_relevant)

        results = search_engine.full_text_search("Python testing pytest")

        # Highly relevant should rank first
        assert len(results) >= 1
        assert results[0].id == highly_relevant.id

    @pytest.mark.asyncio
    async def test_clear_all_indices(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """Clear all removes all indexed documents."""
        doc = (
            await knowledge_manager.create(
                title="To Be Cleared",
                content="This will be cleared from indices.",
                agent="agent",
            )
        ).document
        search_engine.index_document(doc)

        # Verify indexed
        assert len(search_engine.full_text_search("cleared")) >= 1

        # Clear all
        search_engine.clear_all()

        # Should be empty
        assert len(search_engine.full_text_search("cleared")) == 0

    @pytest.mark.asyncio
    async def test_get_stats(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """Get search index statistics."""
        doc = (
            await knowledge_manager.create(
                title="Stats Test",
                content="Document for testing statistics.",
                agent="agent",
            )
        ).document
        search_engine.index_document(doc)

        stats = search_engine.get_stats()

        assert "chunks" in stats
        assert stats["chunks"] >= 1


class TestChromaIndexFilters:
    """Tests for author and path_prefix filters on ChromaIndex.search()."""

    @pytest.mark.asyncio
    async def test_author_filter_includes_matching(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """ChromaIndex.search() returns only docs by the specified author."""
        alice_doc = (
            await knowledge_manager.create(
                title="Alice's Research",
                content="Deep learning architectures and transformer models.",
                agent="alice",
            )
        ).document
        bob_doc = (
            await knowledge_manager.create(
                title="Bob's Research",
                content="Deep learning architectures and transformer models.",
                agent="bob",
            )
        ).document
        search_engine.index_document(alice_doc)
        search_engine.index_document(bob_doc)

        results = search_engine.chroma.search(
            "deep learning transformers", limit=10, threshold=0.0, author="alice"
        )
        result_ids = [r.id for r in results]

        assert alice_doc.id in result_ids
        assert bob_doc.id not in result_ids

    @pytest.mark.asyncio
    async def test_author_filter_excludes_all_when_no_match(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """ChromaIndex.search() returns empty list when author filter matches nobody."""
        doc = (
            await knowledge_manager.create(
                title="Some Research",
                content="Machine learning and neural networks.",
                agent="charlie",
            )
        ).document
        search_engine.index_document(doc)

        results = search_engine.chroma.search(
            "machine learning", limit=10, threshold=0.0, author="nobody"
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_path_prefix_filter_includes_matching(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """ChromaIndex.search() returns only docs under the given path prefix."""
        procedures_doc = (
            await knowledge_manager.create(
                title="Deployment Procedure",
                content="Steps for deploying microservices to production.",
                agent="agent",
                path="procedures",
            )
        ).document
        notes_doc = (
            await knowledge_manager.create(
                title="Deployment Notes",
                content="Steps for deploying microservices to production.",
                agent="agent",
                path="notes",
            )
        ).document
        search_engine.index_document(procedures_doc)
        search_engine.index_document(notes_doc)

        results = search_engine.chroma.search(
            "deploying microservices", limit=10, threshold=0.0, path_prefix="procedures"
        )
        result_ids = [r.id for r in results]

        assert procedures_doc.id in result_ids
        assert notes_doc.id not in result_ids

    @pytest.mark.asyncio
    async def test_author_and_path_prefix_combined(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """ChromaIndex.search() applies author and path_prefix as AND filters."""
        match_doc = (
            await knowledge_manager.create(
                title="Alice Procedures Doc",
                content="Database optimisation and indexing strategies.",
                agent="alice",
                path="procedures",
            )
        ).document
        wrong_author_doc = (
            await knowledge_manager.create(
                title="Bob Procedures Doc",
                content="Database optimisation and indexing strategies.",
                agent="bob",
                path="procedures",
            )
        ).document
        wrong_path_doc = (
            await knowledge_manager.create(
                title="Alice Notes Doc",
                content="Database optimisation and indexing strategies.",
                agent="alice",
                path="notes",
            )
        ).document
        search_engine.index_document(match_doc)
        search_engine.index_document(wrong_author_doc)
        search_engine.index_document(wrong_path_doc)

        results = search_engine.chroma.search(
            "database indexing",
            limit=10,
            threshold=0.0,
            author="alice",
            path_prefix="procedures",
        )
        result_ids = [r.id for r in results]

        assert match_doc.id in result_ids
        assert wrong_author_doc.id not in result_ids
        assert wrong_path_doc.id not in result_ids

    @pytest.mark.asyncio
    async def test_semantic_search_engine_wires_author_filter(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """SearchEngine.semantic_search() correctly wires author filter to ChromaIndex."""
        alice_doc = (
            await knowledge_manager.create(
                title="Alice's ML Notes",
                content="Reinforcement learning policy gradients and reward shaping.",
                agent="alice",
            )
        ).document
        bob_doc = (
            await knowledge_manager.create(
                title="Bob's ML Notes",
                content="Reinforcement learning policy gradients and reward shaping.",
                agent="bob",
            )
        ).document
        search_engine.index_document(alice_doc)
        search_engine.index_document(bob_doc)

        results = search_engine.semantic_search(
            "reinforcement learning", limit=10, threshold=0.0, author="alice"
        )
        result_ids = [r.id for r in results]

        assert alice_doc.id in result_ids
        assert bob_doc.id not in result_ids

    @pytest.mark.asyncio
    async def test_semantic_search_engine_wires_path_prefix_filter(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """SearchEngine.semantic_search() correctly wires path_prefix filter to ChromaIndex."""
        arch_doc = (
            await knowledge_manager.create(
                title="Architecture Doc",
                content="Event-driven architecture with message queues and async processing.",
                agent="agent",
                path="architecture",
            )
        ).document
        other_doc = (
            await knowledge_manager.create(
                title="Other Doc",
                content="Event-driven architecture with message queues and async processing.",
                agent="agent",
                path="scratch",
            )
        ).document
        search_engine.index_document(arch_doc)
        search_engine.index_document(other_doc)

        results = search_engine.semantic_search(
            "event-driven message queues", limit=10, threshold=0.0, path_prefix="architecture"
        )
        result_ids = [r.id for r in results]

        assert arch_doc.id in result_ids
        assert other_doc.id not in result_ids


class TestSearchEngineResiliency:
    """Tests for error propagation and partial-failure handling in SearchEngine."""

    @pytest.mark.asyncio
    async def test_full_text_search_raises_on_backend_error(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """full_text_search raises SearchBackendError when Tantivy errors.

        Callers must be able to distinguish a backend failure from an empty
        result set; silent swallowing of exceptions is not acceptable.
        """

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated tantivy failure")

        search_engine.tantivy.search = _boom  # type: ignore[method-assign]

        with pytest.raises(SearchBackendError) as exc_info:
            search_engine.full_text_search("anything")

        assert "tantivy" in exc_info.value.backend_errors
        assert isinstance(exc_info.value.backend_errors["tantivy"], RuntimeError)

    @pytest.mark.asyncio
    async def test_semantic_search_raises_on_backend_error(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """semantic_search raises SearchBackendError when ChromaDB errors.

        Callers must be able to distinguish a backend failure from an empty
        result set; silent swallowing of exceptions is not acceptable.
        """

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated chroma failure")

        search_engine.chroma.search = _boom  # type: ignore[method-assign]

        with pytest.raises(SearchBackendError) as exc_info:
            search_engine.semantic_search("anything")

        assert "chroma" in exc_info.value.backend_errors
        assert isinstance(exc_info.value.backend_errors["chroma"], RuntimeError)

    @pytest.mark.asyncio
    async def test_search_backend_error_is_lithos_error(self, search_engine: SearchEngine):
        """SearchBackendError is a subclass of LithosError for broad catching."""
        from lithos.errors import LithosError

        def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        search_engine.tantivy.search = _boom  # type: ignore[method-assign]

        with pytest.raises(LithosError):
            search_engine.full_text_search("anything")

    @pytest.mark.asyncio
    async def test_index_document_partial_failure_does_not_raise(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """index_document logs and continues when one backend fails."""

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated failure")

        search_engine.tantivy.add_document = _boom  # type: ignore[method-assign]

        doc = (
            await knowledge_manager.create(
                title="Resilience Test",
                content="Testing partial backend failure during indexing.",
                agent="test-agent",
            )
        ).document
        # Should not raise even though Tantivy is broken
        chunks = search_engine.index_document(doc)
        # Chroma still works, so chunks > 0
        assert chunks >= 1

    @pytest.mark.asyncio
    async def test_index_document_total_failure_raises(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """index_document raises IndexingError when every backend fails."""

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated failure")

        search_engine.tantivy.add_document = _boom  # type: ignore[method-assign]
        search_engine.chroma.add_document = _boom  # type: ignore[method-assign]

        doc = (
            await knowledge_manager.create(
                title="Total Failure Test",
                content="Both backends will fail for this document.",
                agent="test-agent",
            )
        ).document

        with pytest.raises(IndexingError) as exc_info:
            search_engine.index_document(doc)

        assert "tantivy" in exc_info.value.backend_errors
        assert "chroma" in exc_info.value.backend_errors

    @pytest.mark.asyncio
    async def test_remove_document_partial_failure_does_not_raise(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """remove_document logs and continues when one backend fails."""

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated failure")

        search_engine.tantivy.remove_document = _boom  # type: ignore[method-assign]

        doc = (
            await knowledge_manager.create(
                title="Remove Partial Failure",
                content="One backend will fail during removal.",
                agent="test-agent",
            )
        ).document
        search_engine.index_document(doc)

        # Should not raise even though Tantivy remove is broken
        search_engine.remove_document(doc.id)  # no exception

    @pytest.mark.asyncio
    async def test_remove_document_total_failure_raises(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """remove_document raises IndexingError when every backend fails."""

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated failure")

        search_engine.tantivy.remove_document = _boom  # type: ignore[method-assign]
        search_engine.chroma.remove_document = _boom  # type: ignore[method-assign]

        with pytest.raises(IndexingError) as exc_info:
            search_engine.remove_document("fake-doc-id")

        assert "tantivy" in exc_info.value.backend_errors
        assert "chroma" in exc_info.value.backend_errors

    def test_health_returns_ok_when_backends_available(self, search_engine: SearchEngine):
        """health() reports ok for both backends when they are up."""
        status = search_engine.health()
        assert status.get("tantivy") == "ok"
        assert status.get("chroma") == "ok"

    def test_tantivy_open_or_create_recovers_from_corruption(self, tmp_path):
        """open_or_create recreates a corrupted index rather than raising."""
        from lithos.search import TantivyIndex

        index_path = tmp_path / "tantivy"
        index_path.mkdir()

        # Write junk to simulate a corrupted index
        (index_path / "meta.json").write_text("not valid json {{{")

        idx = TantivyIndex(index_path)
        # Should not raise — should wipe and recreate
        idx.open_or_create()
        assert idx._index is not None


class TestSchemaVersionDetection:
    """Tests for US-010: Schema mismatch detection and automatic rebuild."""

    def test_new_index_writes_schema_version(self, tmp_path):
        """Creating a new index writes schema version marker."""
        from lithos.search import TantivyIndex

        index_path = tmp_path / "tantivy"
        idx = TantivyIndex(index_path)
        idx.open_or_create()

        version_file = index_path / ".schema_version"
        assert version_file.exists()
        assert version_file.read_text().strip() == TantivyIndex.SCHEMA_VERSION

    def test_matching_version_no_rebuild(self, tmp_path):
        """Matching version: no rebuild needed."""
        from lithos.search import TantivyIndex

        index_path = tmp_path / "tantivy"
        idx1 = TantivyIndex(index_path)
        idx1.open_or_create()
        assert idx1.needs_rebuild is True  # First time = new index

        idx2 = TantivyIndex(index_path)
        idx2.open_or_create()
        assert idx2.needs_rebuild is False  # Same version, no rebuild

    def test_mismatched_version_triggers_rebuild(self, tmp_path):
        """Mismatched version marker triggers rebuild."""
        from lithos.search import TantivyIndex

        index_path = tmp_path / "tantivy"
        idx1 = TantivyIndex(index_path)
        idx1.open_or_create()

        # Tamper with the version marker
        (index_path / ".schema_version").write_text("old_version")

        idx2 = TantivyIndex(index_path)
        idx2.open_or_create()
        assert idx2.needs_rebuild is True
        # Version marker should be updated
        assert (index_path / ".schema_version").read_text().strip() == TantivyIndex.SCHEMA_VERSION

    def test_missing_version_triggers_rebuild(self, tmp_path):
        """Missing version marker triggers rebuild."""
        from lithos.search import TantivyIndex

        index_path = tmp_path / "tantivy"
        idx1 = TantivyIndex(index_path)
        idx1.open_or_create()

        # Remove the version marker
        (index_path / ".schema_version").unlink()

        idx2 = TantivyIndex(index_path)
        idx2.open_or_create()
        assert idx2.needs_rebuild is True

    def test_rebuild_index_still_functional(self, tmp_path):
        """After schema version rebuild, index is still functional."""
        from lithos.knowledge import KnowledgeDocument, KnowledgeMetadata
        from lithos.search import TantivyIndex

        index_path = tmp_path / "tantivy"
        idx = TantivyIndex(index_path)
        idx.open_or_create()

        # Simulate old schema by tampering
        (index_path / ".schema_version").write_text("1")

        idx2 = TantivyIndex(index_path)
        idx2.open_or_create()
        assert idx2.needs_rebuild is True

        # Index should work after rebuild
        from datetime import datetime, timezone
        from pathlib import Path

        doc = KnowledgeDocument(
            id="test-id",
            title="Test",
            content="Test content.",
            metadata=KnowledgeMetadata(
                id="test-id",
                title="Test",
                author="agent",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ),
            path=Path("test.md"),
        )
        idx2.add_document(doc)
        results = idx2.search("test")
        assert len(results) >= 1


class TestExpiresAtInSearch:
    """Tests for expires_at / is_stale in search index backends."""

    @pytest.mark.asyncio
    async def test_tantivy_expired_doc_is_stale(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """Tantivy search returns is_stale=True for expired doc."""
        from datetime import datetime, timedelta, timezone

        doc = (
            await knowledge_manager.create(
                title="Expired Research",
                content="This research has expired and is stale.",
                agent="agent",
                expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
        ).document
        search_engine.index_document(doc)

        results = search_engine.full_text_search("expired research")
        assert len(results) >= 1
        match = [r for r in results if r.id == doc.id]
        assert len(match) == 1
        assert match[0].is_stale is True

    @pytest.mark.asyncio
    async def test_tantivy_fresh_doc_not_stale(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """Tantivy search returns is_stale=False for fresh doc."""
        from datetime import datetime, timedelta, timezone

        doc = (
            await knowledge_manager.create(
                title="Fresh Research",
                content="This research is still fresh and valid.",
                agent="agent",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            )
        ).document
        search_engine.index_document(doc)

        results = search_engine.full_text_search("fresh research")
        assert len(results) >= 1
        match = [r for r in results if r.id == doc.id]
        assert len(match) == 1
        assert match[0].is_stale is False

    @pytest.mark.asyncio
    async def test_tantivy_no_expires_at_not_stale(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """Tantivy search returns is_stale=False for doc without expires_at."""
        doc = (
            await knowledge_manager.create(
                title="No Expiry Research",
                content="This research has no expiry date set.",
                agent="agent",
            )
        ).document
        search_engine.index_document(doc)

        results = search_engine.full_text_search("no expiry research")
        assert len(results) >= 1
        match = [r for r in results if r.id == doc.id]
        assert len(match) == 1
        assert match[0].is_stale is False

    @pytest.mark.asyncio
    async def test_chroma_expired_doc_is_stale(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """ChromaDB search returns is_stale=True for expired doc."""
        from datetime import datetime, timedelta, timezone

        doc = (
            await knowledge_manager.create(
                title="Expired Semantic Doc",
                content="This document about machine learning has expired.",
                agent="agent",
                expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
        ).document
        search_engine.index_document(doc)

        results = search_engine.semantic_search("machine learning expired")
        match = [r for r in results if r.id == doc.id]
        assert len(match) == 1
        assert match[0].is_stale is True

    @pytest.mark.asyncio
    async def test_chroma_fresh_doc_not_stale(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """ChromaDB search returns is_stale=False for fresh doc."""
        from datetime import datetime, timedelta, timezone

        doc = (
            await knowledge_manager.create(
                title="Fresh Semantic Doc",
                content="This document about deep learning is still fresh.",
                agent="agent",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            )
        ).document
        search_engine.index_document(doc)

        results = search_engine.semantic_search("deep learning fresh")
        match = [r for r in results if r.id == doc.id]
        assert len(match) == 1
        assert match[0].is_stale is False

    @pytest.mark.asyncio
    async def test_chroma_no_expires_at_not_stale(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """ChromaDB search returns is_stale=False for doc without expires_at."""
        doc = (
            await knowledge_manager.create(
                title="No Expiry Semantic Doc",
                content="This document about neural networks has no expiry.",
                agent="agent",
            )
        ).document
        search_engine.index_document(doc)

        results = search_engine.semantic_search("neural networks no expiry")
        match = [r for r in results if r.id == doc.id]
        assert len(match) == 1
        assert match[0].is_stale is False


class TestHybridSearch:
    """Tests for hybrid search (RRF fusion of Tantivy + ChromaDB)."""

    def test_rrf_pure_function(self):
        """reciprocal_rank_fusion produces correct scores."""
        # Two lists with one common doc: common doc should rank highest
        list1 = ["common", "only_in_1"]
        list2 = ["common", "only_in_2"]
        scores = reciprocal_rank_fusion([list1, list2])

        assert "common" in scores
        assert "only_in_1" in scores
        assert "only_in_2" in scores
        # common appears in both lists so its score should be highest
        assert scores["common"] > scores["only_in_1"]
        assert scores["common"] > scores["only_in_2"]

        # Single list: doc at rank 1 → score = 1/(60+1)
        single = reciprocal_rank_fusion([["doc_a"]])
        assert abs(single["doc_a"] - 1.0 / 61) < 1e-9

        # Empty lists return empty dict
        assert reciprocal_rank_fusion([]) == {}
        assert reciprocal_rank_fusion([[]]) == {}

    @pytest.mark.asyncio
    async def test_hybrid_mode_returns_results(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """hybrid_search finds an indexed document."""
        doc = (
            await knowledge_manager.create(
                title="Hybrid Search Test",
                content="This document is about distributed systems and consensus algorithms.",
                agent="agent",
            )
        ).document
        search_engine.index_document(doc)

        results = search_engine.hybrid_search("distributed systems consensus")

        assert len(results) >= 1
        assert any(r.id == doc.id for r in results)
        # All scores should be positive RRF scores
        for r in results:
            assert r.score > 0

    @pytest.mark.asyncio
    async def test_fulltext_mode_via_engine(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """full_text_search still works independently."""
        doc = (
            await knowledge_manager.create(
                title="Fulltext Only Test",
                content="Searching with BM25 full text retrieval.",
                agent="agent",
            )
        ).document
        search_engine.index_document(doc)

        results = search_engine.full_text_search("BM25 full text")

        assert len(results) >= 1
        assert any(r.id == doc.id for r in results)

    @pytest.mark.asyncio
    async def test_hybrid_deduplicates_by_doc_id(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """Same doc appearing in both backends shows up only once in hybrid results."""
        doc = (
            await knowledge_manager.create(
                title="Deduplication Test",
                content="Python programming language features and best practices.",
                agent="agent",
                tags=["python"],
            )
        ).document
        search_engine.index_document(doc)

        results = search_engine.hybrid_search("Python programming")

        # doc should appear at most once
        ids = [r.id for r in results]
        assert ids.count(doc.id) <= 1
