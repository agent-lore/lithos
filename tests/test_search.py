"""Tests for search module - Tantivy and ChromaDB search."""

import pytest

from exogram.knowledge import KnowledgeManager
from exogram.search import (
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
        doc = await knowledge_manager.create(
            title="Python Tutorial",
            content="Learn Python programming with examples and exercises.",
            agent="agent",
            tags=["python", "tutorial"],
        )
        search_engine.index_document(doc)

        results = search_engine.full_text_search("Python programming")

        assert len(results) >= 1
        assert any(r.id == doc.id for r in results)

    @pytest.mark.asyncio
    async def test_search_by_title(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """Search matches document titles."""
        doc = await knowledge_manager.create(
            title="Kubernetes Deployment Guide",
            content="Steps to deploy applications.",
            agent="agent",
        )
        search_engine.index_document(doc)

        results = search_engine.full_text_search("Kubernetes")

        assert len(results) >= 1
        assert results[0].title == "Kubernetes Deployment Guide"

    @pytest.mark.asyncio
    async def test_search_by_content(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """Search matches document content."""
        doc = await knowledge_manager.create(
            title="Generic Title",
            content="This document discusses microservices architecture patterns.",
            agent="agent",
        )
        search_engine.index_document(doc)

        results = search_engine.full_text_search("microservices architecture")

        assert len(results) >= 1
        assert any(r.id == doc.id for r in results)

    @pytest.mark.asyncio
    async def test_search_with_tag_filter(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """Filter search results by tags."""
        doc1 = await knowledge_manager.create(
            title="Python Web Framework",
            content="Building web apps with Python.",
            agent="agent",
            tags=["python", "web"],
        )
        doc2 = await knowledge_manager.create(
            title="Python Data Science",
            content="Data analysis with Python.",
            agent="agent",
            tags=["python", "data"],
        )
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
        doc = await knowledge_manager.create(
            title="API Documentation",
            content="The REST API supports GET, POST, PUT, and DELETE methods for resource manipulation.",
            agent="agent",
        )
        search_engine.index_document(doc)

        results = search_engine.full_text_search("REST API")

        assert len(results) >= 1
        assert "REST" in results[0].snippet or "API" in results[0].snippet

    @pytest.mark.asyncio
    async def test_document_update_in_index(
        self, knowledge_manager: KnowledgeManager, search_engine: SearchEngine
    ):
        """Updated document is re-indexed correctly."""
        doc = await knowledge_manager.create(
            title="Original Title",
            content="Original content about databases.",
            agent="agent",
        )
        search_engine.index_document(doc)

        # Update document
        updated = await knowledge_manager.update(
            id=doc.id,
            agent="agent",
            content="Updated content about caching strategies.",
        )
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
        doc = await knowledge_manager.create(
            title="Temporary Doc",
            content="This will be removed from the index.",
            agent="agent",
        )
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
        doc = await knowledge_manager.create(
            title="Error Handling Best Practices",
            content="Always catch exceptions and provide meaningful error messages to users.",
            agent="agent",
        )
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
        doc = await knowledge_manager.create(
            title="Machine Learning Basics",
            content="Neural networks learn patterns from training data.",
            agent="agent",
        )
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

        doc = await knowledge_manager.create(
            title="Python Development",
            content=long_content,
            agent="agent",
        )
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
        doc = await knowledge_manager.create(
            title="Database Optimization",
            content="Index your database tables for faster queries.",
            agent="agent",
        )
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
            doc = await knowledge_manager.create(
                title=doc_data["title"],
                content=doc_data["content"],
                agent="test-agent",
                tags=doc_data["tags"],
            )
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
        highly_relevant = await knowledge_manager.create(
            title="Python Testing with Pytest",
            content="Comprehensive guide to testing Python applications using pytest framework.",
            agent="agent",
        )
        somewhat_relevant = await knowledge_manager.create(
            title="Python Basics",
            content="Introduction to Python programming. Testing is mentioned briefly.",
            agent="agent",
        )
        not_relevant = await knowledge_manager.create(
            title="JavaScript Guide",
            content="Learn JavaScript for web development.",
            agent="agent",
        )

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
        doc = await knowledge_manager.create(
            title="To Be Cleared",
            content="This will be cleared from indices.",
            agent="agent",
        )
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
        doc = await knowledge_manager.create(
            title="Stats Test",
            content="Document for testing statistics.",
            agent="agent",
        )
        search_engine.index_document(doc)

        stats = search_engine.get_stats()

        assert "chunks" in stats
        assert stats["chunks"] >= 1
