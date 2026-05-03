"""Conformance tests for Phase 4 freshness features.

These tests prove end-to-end correctness of the research cache and freshness
system, satisfying the conformance test matrix in final-architecture-guardrails.md.
"""

from datetime import datetime, timedelta, timezone

import pytest

from lithos.knowledge import KnowledgeManager
from lithos.search import TantivyIndex
from lithos.server import LithosServer

pytestmark = pytest.mark.integration


class TestFreshnessWriteConformance:
    """Conformance: freshness write round-trips."""

    @pytest.mark.asyncio
    async def test_create_with_ttl_hours_round_trip(self, server: LithosServer):
        """Create with ttl_hours, read back, verify expires_at in metadata."""
        expires = datetime.now(timezone.utc) + timedelta(hours=24)
        result = await server.knowledge.create(
            title="TTL Round Trip",
            content="Content with TTL.",
            agent="agent",
            expires_at=expires,
        )
        assert result.status == "created"
        doc = result.document
        assert doc is not None

        read_doc, _ = await server.knowledge.read(id=doc.id)
        assert read_doc.metadata.expires_at is not None
        assert abs((read_doc.metadata.expires_at - expires).total_seconds()) < 1

    @pytest.mark.asyncio
    async def test_create_with_explicit_expires_at_round_trip(self, server: LithosServer):
        """Create with explicit expires_at, read back, verify match."""
        expires = datetime(2030, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = await server.knowledge.create(
            title="Explicit Expiry",
            content="Content with explicit expiry.",
            agent="agent",
            expires_at=expires,
        )
        doc = result.document
        assert doc is not None

        read_doc, _ = await server.knowledge.read(id=doc.id)
        assert read_doc.metadata.expires_at == expires

    @pytest.mark.asyncio
    async def test_create_via_tool_with_ttl_hours(self, server: LithosServer):
        """MCP-boundary: lithos_write with ttl_hours sets expires_at correctly."""
        tool = await server.mcp.get_tool("lithos_write")
        result = await tool.fn(
            title="TTL via MCP",
            content="Content created via MCP boundary with TTL.",
            agent="agent",
            ttl_hours=12.0,
        )
        assert result["status"] == "created"

        doc, _ = await server.knowledge.read(id=result["id"])
        assert doc.metadata.expires_at is not None
        delta = (doc.metadata.expires_at - datetime.now(timezone.utc)).total_seconds()
        assert 11 * 3600 < delta < 13 * 3600

    @pytest.mark.asyncio
    async def test_expires_at_normalized_to_utc(self, server: LithosServer):
        """MCP-boundary: non-UTC expires_at is normalized to UTC on write."""
        tool = await server.mcp.get_tool("lithos_write")
        # +05:30 offset
        result = await tool.fn(
            title="TZ Normalize",
            content="Content with non-UTC expires_at.",
            agent="agent",
            expires_at="2030-06-15T12:00:00+05:30",
        )
        assert result["status"] == "created"

        doc, _ = await server.knowledge.read(id=result["id"])
        assert doc.metadata.expires_at is not None
        assert doc.metadata.expires_at.tzinfo is not None
        # 12:00 +05:30 = 06:30 UTC
        assert doc.metadata.expires_at.hour == 6
        assert doc.metadata.expires_at.minute == 30


class TestMutualExclusionConformance:
    """Conformance: ttl_hours + expires_at mutual exclusion."""

    @pytest.mark.asyncio
    async def test_both_ttl_and_expires_at_returns_error(self, server: LithosServer):
        """ttl_hours + expires_at together returns invalid_input."""
        tool = await server.mcp.get_tool("lithos_write")
        result = await tool.fn(
            title="Mutual Exclusion",
            content="Should fail.",
            agent="agent",
            ttl_hours=24.0,
            expires_at="2030-01-01T00:00:00+00:00",
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"
        assert "either" in result["message"].lower() or "not both" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_ttl_hours_with_empty_expires_at_returns_error(self, server: LithosServer):
        """ttl_hours + expires_at='' is contradictory and returns invalid_input."""
        tool = await server.mcp.get_tool("lithos_write")
        result = await tool.fn(
            title="Mutual Exclusion Empty",
            content="Should fail.",
            agent="agent",
            ttl_hours=24.0,
            expires_at="",
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"


class TestUpdateConformance:
    """Conformance: update preserve and clear."""

    @pytest.mark.asyncio
    async def test_update_preserves_expires_at(self, server: LithosServer):
        """Update without freshness args preserves existing expires_at."""
        expires = datetime.now(timezone.utc) + timedelta(hours=48)
        doc = (
            await server.knowledge.create(
                title="Preserve Conformance",
                content="Original.",
                agent="agent",
                expires_at=expires,
            )
        ).document
        assert doc is not None

        updated = (
            await server.knowledge.update(
                id=doc.id,
                agent="editor",
                content="Updated content.",
            )
        ).document
        assert updated is not None
        assert updated.metadata.expires_at == expires

    @pytest.mark.asyncio
    async def test_update_clears_expires_at(self, server: LithosServer):
        """Update with expires_at=None clears existing expiry."""
        expires = datetime.now(timezone.utc) + timedelta(hours=48)
        doc = (
            await server.knowledge.create(
                title="Clear Conformance",
                content="Original.",
                agent="agent",
                expires_at=expires,
            )
        ).document
        assert doc is not None

        updated = (
            await server.knowledge.update(
                id=doc.id,
                agent="editor",
                expires_at=None,
            )
        ).document
        assert updated is not None
        assert updated.metadata.expires_at is None


class TestStalenessInSearchConformance:
    """Conformance: staleness in search results."""

    @pytest.mark.asyncio
    async def test_expired_doc_is_stale_in_search(self, server: LithosServer):
        """Expired doc shows is_stale=True in lithos_search results."""
        doc = (
            await server.knowledge.create(
                title="Search Stale Conformance",
                content="This document is expired for search testing.",
                agent="agent",
                expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
        ).document
        server.search.index(KnowledgeManager.to_indexable(doc))

        results = server.search.full_text_search("search stale conformance")
        match = [r for r in results if r.id == doc.id]
        assert len(match) == 1
        assert match[0].is_stale is True

    @pytest.mark.asyncio
    async def test_expired_doc_is_stale_in_semantic(self, server: LithosServer):
        """Expired doc shows is_stale=True in lithos_semantic results."""
        doc = (
            await server.knowledge.create(
                title="Semantic Stale Conformance",
                content="This document about machine learning algorithms is expired.",
                agent="agent",
                expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
        ).document
        server.search.index(KnowledgeManager.to_indexable(doc))

        results = server.search.semantic_search("machine learning algorithms expired")
        match = [r for r in results if r.id == doc.id]
        assert len(match) == 1
        assert match[0].is_stale is True


class TestCacheLookupConformance:
    """Conformance: cache lookup hit/stale/miss."""

    async def _call_cache_lookup(self, server: LithosServer, **kwargs) -> dict:
        tool = await server.mcp.get_tool("lithos_cache_lookup")
        return await tool.fn(**kwargs)

    @pytest.mark.asyncio
    async def test_cache_hit_fresh_doc(self, server: LithosServer):
        """Fresh doc returns hit=True with full content."""
        doc = (
            await server.knowledge.create(
                title="Cache Hit Conformance",
                content="Fresh content about distributed systems.",
                agent="agent",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            )
        ).document
        server.search.index(KnowledgeManager.to_indexable(doc))

        result = await self._call_cache_lookup(server, query="distributed systems")
        assert result["hit"] is True
        assert result["document"] is not None
        assert result["document"]["id"] == doc.id
        assert result["document"]["content"] == "Fresh content about distributed systems."

    @pytest.mark.asyncio
    async def test_cache_stale_expired_doc(self, server: LithosServer):
        """Expired doc returns hit=False, stale_exists=True, stale_id."""
        doc = (
            await server.knowledge.create(
                title="Cache Stale Conformance",
                content="Expired content about graph databases.",
                agent="agent",
                expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
        ).document
        server.search.index(KnowledgeManager.to_indexable(doc))

        result = await self._call_cache_lookup(server, query="graph databases")
        assert result["hit"] is False
        assert result["stale_exists"] is True
        assert result["stale_id"] == doc.id

    @pytest.mark.asyncio
    async def test_cache_clean_miss(self, server: LithosServer):
        """No matching doc returns hit=False, stale_exists=False."""
        result = await self._call_cache_lookup(server, query="completely unique topic zzzxxx999")
        assert result["hit"] is False
        assert result["document"] is None
        assert result["stale_exists"] is False
        assert result["stale_id"] is None


class TestStaleUpdateFlowConformance:
    """Conformance: stale update flow."""

    async def _call_cache_lookup(self, server: LithosServer, **kwargs) -> dict:
        tool = await server.mcp.get_tool("lithos_cache_lookup")
        return await tool.fn(**kwargs)

    @pytest.mark.asyncio
    async def test_stale_update_flow(self, server: LithosServer):
        """Create doc with short TTL, get stale_id, update, get hit."""
        # Create with already-expired TTL
        doc = (
            await server.knowledge.create(
                title="Stale Flow Doc",
                content="Old content about serverless computing.",
                agent="agent",
                expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        ).document
        server.search.index(KnowledgeManager.to_indexable(doc))

        # Cache lookup should return stale
        result1 = await self._call_cache_lookup(server, query="serverless computing")
        assert result1["hit"] is False
        assert result1["stale_exists"] is True
        stale_id = result1["stale_id"]

        # Update the stale doc with new content and fresh TTL
        updated = (
            await server.knowledge.update(
                id=stale_id,
                agent="refresher",
                content="Updated fresh content about serverless computing.",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            )
        ).document
        server.search.index(KnowledgeManager.to_indexable(updated))

        # Cache lookup should now return hit
        result2 = await self._call_cache_lookup(server, query="serverless computing")
        assert result2["hit"] is True
        assert result2["document"]["id"] == stale_id


class TestSourceUrlFastPathConformance:
    """Conformance: source_url fast path."""

    async def _call_cache_lookup(self, server: LithosServer, **kwargs) -> dict:
        tool = await server.mcp.get_tool("lithos_cache_lookup")
        return await tool.fn(**kwargs)

    @pytest.mark.asyncio
    async def test_source_url_fast_path(self, server: LithosServer):
        """lithos_cache_lookup with source_url returns exact URL match."""
        doc = (
            await server.knowledge.create(
                title="Fast Path Doc",
                content="Content from a specific URL source.",
                agent="agent",
                source_url="https://example.com/fast-path-test",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            )
        ).document
        server.search.index(KnowledgeManager.to_indexable(doc))

        result = await self._call_cache_lookup(
            server,
            query="fast path content",
            source_url="https://example.com/fast-path-test",
        )
        assert result["hit"] is True
        assert result["document"]["id"] == doc.id
        assert result["document"]["source_url"] == "https://example.com/fast-path-test"


class TestOnDiskCompatibilityConformance:
    """Conformance: on-disk compatibility."""

    @pytest.mark.asyncio
    async def test_doc_without_expires_at(self, server: LithosServer):
        """Document without expires_at loads correctly, is_stale=False."""
        doc = (
            await server.knowledge.create(
                title="No Expiry Doc",
                content="Content without any freshness metadata.",
                agent="agent",
            )
        ).document

        read_doc, _ = await server.knowledge.read(id=doc.id)
        assert read_doc.metadata.expires_at is None
        assert read_doc.metadata.is_stale is False

    @pytest.mark.asyncio
    async def test_doc_without_expires_at_in_cache_lookup(self, server: LithosServer):
        """Document without expires_at can be matched by cache lookup."""
        doc = (
            await server.knowledge.create(
                title="Compatible Legacy Doc",
                content="Legacy content about container orchestration.",
                agent="agent",
                confidence=0.9,
            )
        ).document
        server.search.index(KnowledgeManager.to_indexable(doc))

        tool = await server.mcp.get_tool("lithos_cache_lookup")
        result = await tool.fn(query="container orchestration")
        # Should be a hit (no expires_at means never stale)
        assert result["hit"] is True
        assert result["document"]["id"] == doc.id


class TestSchemaConformance:
    """Conformance: schema version and rebuild."""

    def test_schema_version_not_bumped(self):
        """Phase 4 does not bump Tantivy SCHEMA_VERSION."""
        # expires_at field already existed in the schema
        assert TantivyIndex.SCHEMA_VERSION == "3"

    @pytest.mark.asyncio
    async def test_startup_rebuild_path(self, server: LithosServer, test_config):
        """Simulate schema mismatch, verify rebuild succeeds with freshness data."""
        # Create a doc with expires_at
        doc = (
            await server.knowledge.create(
                title="Rebuild Test Doc",
                content="Content for rebuild testing.",
                agent="agent",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            )
        ).document
        server.search.index(KnowledgeManager.to_indexable(doc))

        # Corrupt the schema version marker to simulate mismatch
        index_path = test_config.storage.tantivy_path
        version_file = index_path / ".schema_version"
        if version_file.exists():
            version_file.write_text("0")  # Force mismatch

        # Recreate the TantivyIndex — it should detect mismatch and rebuild
        new_tantivy = TantivyIndex(index_path)
        new_tantivy.open_or_create()
        assert new_tantivy.needs_rebuild is True

        # Re-index the document
        new_tantivy.add_document(doc)

        # Search should return correct results including is_stale
        results = new_tantivy.search("rebuild testing")
        assert len(results) >= 1
        match = [r for r in results if r.id == doc.id]
        assert len(match) == 1
        # Doc has future expires_at, so should not be stale
        assert match[0].is_stale is False
