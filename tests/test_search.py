"""Tests for search module - Tantivy and ChromaDB search."""

import pytest

from lithos.errors import IndexingError, SearchBackendError
from lithos.knowledge import KnowledgeManager
from lithos.search import (
    SearchEngine,
    chunk_text,
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
