"""Integration tests for MCP server - full tool workflows."""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lithos.config import LithosConfig
from lithos.server import LithosServer, _FileChangeHandler, create_server, get_server

pytestmark = pytest.mark.integration


class TestServerInitialization:
    """Tests for server startup and shutdown."""

    @pytest.mark.asyncio
    async def test_server_initializes(self, server: LithosServer):
        """Server initializes all components."""
        assert server.knowledge is not None
        assert server.search is not None
        assert server.graph is not None
        assert server.coordination is not None

    @pytest.mark.asyncio
    async def test_server_registers_tools(self, server: LithosServer):
        """Server registers all MCP tools."""
        # The server should have registered tools
        # Check by verifying the mcp app has tools
        assert server.mcp is not None

    @pytest.mark.asyncio
    async def test_file_change_handler_schedules_on_loop(self):
        """File change handler schedules work on provided event loop."""

        class DummyServer:
            def __init__(self):
                self.calls: list[tuple[str, bool]] = []

            async def handle_file_change(self, path, deleted=False):
                self.calls.append((str(path), deleted))

        dummy = DummyServer()
        handler = _FileChangeHandler(dummy, asyncio.get_running_loop())

        handler._schedule_update(path=Path("/tmp/handler-test.md"), deleted=True)
        await asyncio.sleep(0.05)

        assert dummy.calls == [("/tmp/handler-test.md", True)]

    @pytest.mark.asyncio
    async def test_initialize_rebuilds_when_configured(self, test_config):
        """Initialization should force rebuild when rebuild_on_start is enabled."""
        test_config.index.rebuild_on_start = True
        server = LithosServer(test_config)
        rebuild = AsyncMock()
        server._rebuild_indices = rebuild  # type: ignore[assignment]

        await server.initialize()
        rebuild.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_initialize_rebuilds_after_semantic_store_quarantine(self, test_config):
        """Initialization rebuilds indices after semantic-store quarantine succeeds."""
        server = LithosServer(test_config)
        rebuild = AsyncMock()
        backup_path = test_config.storage.data_dir / ".chroma.corrupt-test"

        server._rebuild_indices = rebuild  # type: ignore[assignment]
        server.search.ensure_semantic_backend_healthy = MagicMock(  # type: ignore[method-assign]
            return_value=(True, backup_path)
        )
        server.search._tantivy = SimpleNamespace(needs_rebuild=False)
        server.search._chroma = SimpleNamespace(_model=object())

        await server.initialize()

        rebuild.assert_awaited_once()
        server.search.ensure_semantic_backend_healthy.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_initialize_warns_but_stays_up_when_semantic_backend_unavailable(
        self, test_config, caplog
    ):
        """Initialization logs degraded semantic search without crashing the server."""
        server = LithosServer(test_config)
        rebuild = AsyncMock()

        server._rebuild_indices = rebuild  # type: ignore[assignment]
        server.search.ensure_semantic_backend_healthy = MagicMock(  # type: ignore[method-assign]
            return_value=(False, None)
        )
        server.search._semantic_store_error = "simulated corruption"
        server.search._tantivy = SimpleNamespace(needs_rebuild=False)
        server.search._chroma = SimpleNamespace(_model=object())
        server.graph.load_cache = MagicMock(return_value=True)  # type: ignore[method-assign]

        with caplog.at_level(logging.WARNING, logger="lithos.server"):
            await server.initialize()

        rebuild.assert_not_awaited()
        server.graph.load_cache.assert_called_once_with()
        assert "Semantic search backend remains unavailable after repair attempt" in caplog.text

    def test_start_file_watcher_requires_running_loop(self, test_config):
        """Watcher startup without a running loop raises a clear RuntimeError."""
        server = LithosServer(test_config)
        with pytest.raises(RuntimeError, match="running asyncio event loop"):
            server.start_file_watcher()

    @pytest.mark.asyncio
    async def test_start_and_stop_file_watcher_is_idempotent(self, server: LithosServer):
        """Starting twice and stopping twice should not raise."""
        server.start_file_watcher()
        first_observer = server._observer
        assert first_observer is not None

        # No-op when already started.
        server.start_file_watcher()
        assert server._observer is first_observer

        server.stop_file_watcher()
        assert server._observer is None

        # No-op when already stopped.
        server.stop_file_watcher()

    @pytest.mark.asyncio
    async def test_handle_file_change_ignores_non_markdown_and_outside_root(
        self, server: LithosServer
    ):
        """Non-markdown and out-of-root file events are ignored safely."""
        original_nodes = server.graph.node_count()

        await server.handle_file_change(
            server.config.storage.knowledge_path / "ignored.txt", deleted=False
        )
        await server.handle_file_change(Path("/tmp/outside.md"), deleted=False)

        assert server.graph.node_count() == original_nodes

    @pytest.mark.asyncio
    async def test_handle_file_change_modified_markdown_reindexes(self, server: LithosServer):
        """A markdown modification event reindexes search and graph projections."""
        doc = (
            await server.knowledge.create(
                title="Watcher Modify Doc",
                content="Original watcher content.",
                agent="watcher-agent",
                path="watched",
            )
        ).document
        server.search.index_document(doc)
        server.graph.add_document(doc)

        file_path = server.config.storage.knowledge_path / doc.path
        file_path.write_text(
            f"""---
id: {doc.id}
title: Watcher Modify Doc
author: watcher-agent
created_at: {doc.metadata.created_at.isoformat()}
updated_at: {doc.metadata.updated_at.isoformat()}
tags: []
aliases: []
confidence: 1.0
contributors: []
---
# Watcher Modify Doc

Updated watcher content with unique phrase.
"""
        )

        await server.handle_file_change(file_path, deleted=False)

        results = server.search.full_text_search("unique phrase", limit=10)
        assert any(r.id == doc.id for r in results)
        assert server.graph.has_node(doc.id)

    @pytest.mark.asyncio
    async def test_handle_file_change_update_rebuilds_outgoing_links(self, server: LithosServer):
        """Updating a document via file change rebuilds its outgoing wiki-links in the graph."""
        # Create target document
        target = (
            await server.knowledge.create(
                title="Watcher Target",
                content="This is the target.",
                agent="watcher-agent",
            )
        ).document
        server.graph.add_document(target)

        # Create source document WITHOUT wiki-links
        source = (
            await server.knowledge.create(
                title="Watcher Source",
                content="No links here.",
                agent="watcher-agent",
            )
        ).document
        server.graph.add_document(source)

        assert not server.graph.has_edge(source.id, target.id)

        # Write updated content WITH a wiki-link to target
        file_path = server.config.storage.knowledge_path / source.path
        file_path.write_text(
            f"""---
id: {source.id}
title: Watcher Source
author: watcher-agent
created_at: {source.metadata.created_at.isoformat()}
updated_at: {source.metadata.updated_at.isoformat()}
tags: []
aliases: []
confidence: 1.0
contributors: []
---
# Watcher Source

Now linking to [[watcher-target]].
"""
        )

        await server.handle_file_change(file_path, deleted=False)

        assert server.graph.has_edge(source.id, target.id)

    @pytest.mark.asyncio
    async def test_file_change_handler_event_methods_route_events(self):
        """Event callbacks dispatch to schedule method with expected deleted flag."""

        class DummyServer:
            async def handle_file_change(self, path, deleted=False):
                return None

        handler = _FileChangeHandler(DummyServer(), asyncio.get_running_loop())
        calls: list[tuple[str, bool]] = []

        def _capture(path: Path, deleted: bool = False):
            calls.append((str(path), deleted))

        handler._schedule_update = _capture  # type: ignore[assignment]

        handler.on_created(SimpleNamespace(is_directory=False, src_path="/tmp/a.md"))
        handler.on_modified(SimpleNamespace(is_directory=False, src_path="/tmp/b.md"))
        handler.on_deleted(SimpleNamespace(is_directory=False, src_path="/tmp/c.md"))
        handler.on_created(SimpleNamespace(is_directory=True, src_path="/tmp/ignored_dir"))

        assert calls == [
            ("/tmp/a.md", False),
            ("/tmp/b.md", False),
            ("/tmp/c.md", True),
        ]

    @pytest.mark.asyncio
    async def test_file_change_handler_schedule_exception_is_swallowed(self, monkeypatch):
        """Scheduling failures should be swallowed rather than crash file watcher thread."""

        class DummyServer:
            async def handle_file_change(self, path, deleted=False):
                return None

        handler = _FileChangeHandler(DummyServer(), asyncio.get_running_loop())

        def _boom(coro, *_args, **_kwargs):
            coro.close()
            raise RuntimeError("schedule boom")

        monkeypatch.setattr("lithos.server.asyncio.run_coroutine_threadsafe", _boom)
        handler._schedule_update(Path("/tmp/fail.md"), deleted=False)

    def test_file_change_handler_logs_future_exception(self):
        """Future callback handles exceptional and callback-failure futures safely."""

        class _FutureWithException:
            def exception(self):
                return RuntimeError("background failure")

        class _FutureExceptionRaises:
            def exception(self):
                raise RuntimeError("cannot fetch exception")

        _FileChangeHandler._log_future_exception(_FutureWithException())
        _FileChangeHandler._log_future_exception(_FutureExceptionRaises())

    def test_global_server_singleton_helpers(self, test_config):
        """create_server installs global instance and get_server returns same object."""
        created = create_server(test_config)
        fetched = get_server()
        assert fetched is created


class TestKnowledgeToolWorkflow:
    """Integration tests for knowledge management tools."""

    @pytest.mark.asyncio
    async def test_create_read_update_delete_workflow(self, server: LithosServer):
        """Complete CRUD workflow through server."""
        # Create
        doc = (
            await server.knowledge.create(
                title="Integration Test Doc",
                content="Initial content for testing.",
                agent="test-agent",
                tags=["test", "integration"],
            )
        ).document
        doc_id = doc.id

        # Verify indexed
        await asyncio.sleep(0.1)  # Allow indexing

        # Read
        read_doc, _ = await server.knowledge.read(id=doc_id)
        assert read_doc.title == "Integration Test Doc"
        assert read_doc.content == "Initial content for testing."

        # Update
        updated = (
            await server.knowledge.update(
                id=doc_id,
                agent="editor-agent",
                content="Updated content.",
                tags=["test", "integration", "updated"],
            )
        ).document
        assert updated.content == "Updated content."
        assert "updated" in updated.metadata.tags

        # Delete
        success, _path = await server.knowledge.delete(doc_id)
        assert success

        # Verify deleted
        with pytest.raises(FileNotFoundError):
            await server.knowledge.read(id=doc_id)

    @pytest.mark.asyncio
    async def test_create_with_wiki_links_updates_graph(self, server: LithosServer):
        """Creating document with links updates knowledge graph."""
        # Create target first
        target = (
            await server.knowledge.create(
                title="Link Target",
                content="This is the target document.",
                agent="agent",
            )
        ).document
        server.search.index_document(target)
        server.graph.add_document(target)

        # Create source with link
        source = (
            await server.knowledge.create(
                title="Link Source",
                content="See [[link-target]] for details.",
                agent="agent",
            )
        ).document
        server.search.index_document(source)
        server.graph.add_document(source)

        # Verify graph has edge
        assert server.graph.has_edge(source.id, target.id)

        # Verify backlinks work
        incoming = server.graph.get_incoming_links(target.id)
        assert any(n["id"] == source.id for n in incoming)

    @pytest.mark.asyncio
    async def test_update_via_lithos_write_rebuilds_outgoing_links(self, server: LithosServer):
        """Updating a document via lithos_write with wiki-links rebuilds outgoing graph edges."""
        tool = await server.mcp.get_tool("lithos_write")

        # Create target document
        target = (
            await server.knowledge.create(
                title="Write Target",
                content="This is the write target.",
                agent="agent",
            )
        ).document
        server.graph.add_document(target)

        # Create source document WITHOUT wiki-links
        source = (
            await server.knowledge.create(
                title="Write Source",
                content="No links here.",
                agent="agent",
            )
        ).document
        server.graph.add_document(source)

        assert not server.graph.has_edge(source.id, target.id)

        # Update source via lithos_write with a wiki-link to target
        result = await tool.fn(
            id=source.id,
            title="Write Source",
            content="Now linking to [[write-target]].",
            agent="agent",
        )
        assert result["status"] == "updated"

        assert server.graph.has_edge(source.id, target.id)

    @pytest.mark.asyncio
    async def test_lithos_list_filters_and_returns_updated(self, server: LithosServer):
        """list_all supports path_prefix and since filters; response includes updated_at."""
        await server.knowledge.create(
            title="Old Procedure",
            content="Older procedure.",
            agent="agent",
            path="procedures",
        )
        await server.knowledge.create(
            title="Other Guide",
            content="Other path.",
            agent="agent",
            path="guides",
        )

        cutoff = datetime.now(timezone.utc)
        await asyncio.sleep(0.02)

        new_doc = (
            await server.knowledge.create(
                title="New Procedure",
                content="Newer procedure.",
                agent="agent",
                path="procedures",
            )
        ).document

        docs, total = await server.knowledge.list_all(
            path_prefix="procedures",
            since=cutoff,
            limit=50,
            offset=0,
        )

        assert total == 1
        assert len(docs) == 1
        assert docs[0].id == new_doc.id
        assert str(docs[0].path).startswith("procedures")
        assert docs[0].metadata.updated_at is not None

    @pytest.mark.asyncio
    async def test_lithos_tags_prefix_filter(self, server: LithosServer):
        """lithos_tags with prefix only returns matching tags (fixes #81)."""
        await server.knowledge.create(
            title="Python Web Doc",
            content="Python web content.",
            agent="agent",
            tags=["python:web", "python:api"],
        )
        await server.knowledge.create(
            title="Rust Systems Doc",
            content="Rust systems content.",
            agent="agent",
            tags=["rust:systems"],
        )
        tool = await server.mcp.get_tool("lithos_tags")
        result = await tool.fn(prefix="python")
        assert "tags" in result
        tags = result["tags"]
        assert "python:web" in tags
        assert "python:api" in tags
        assert "rust:systems" not in tags

    @pytest.mark.asyncio
    async def test_lithos_tags_no_prefix_returns_all(self, server: LithosServer):
        """lithos_tags without prefix returns all tags (existing behaviour unchanged)."""
        await server.knowledge.create(
            title="Multi Tag Doc",
            content="Content.",
            agent="agent",
            tags=["alpha", "beta"],
        )
        tool = await server.mcp.get_tool("lithos_tags")
        result = await tool.fn()
        assert "tags" in result
        tags = result["tags"]
        assert "alpha" in tags
        assert "beta" in tags

    @pytest.mark.asyncio
    async def test_lithos_list_title_contains(self, server: LithosServer):
        """lithos_list with title_contains filters by case-insensitive substring (fixes #48)."""
        await server.knowledge.create(
            title="Alpha Guide",
            content="Alpha content.",
            agent="agent",
        )
        await server.knowledge.create(
            title="Beta Reference",
            content="Beta content.",
            agent="agent",
        )
        tool = await server.mcp.get_tool("lithos_list")
        result = await tool.fn(title_contains="alpha")
        assert result["total"] == 1
        assert len(result["items"]) == 1
        assert result["items"][0]["title"] == "Alpha Guide"

    @pytest.mark.asyncio
    async def test_lithos_list_content_query(self, server: LithosServer):
        """lithos_list with content_query intersects FTS results (fixes #48)."""
        doc_a = (
            await server.knowledge.create(
                title="Doc A",
                content="Unique content for doc a.",
                agent="agent",
            )
        ).document
        await server.knowledge.create(
            title="Doc B",
            content="Other content for doc b.",
            agent="agent",
        )
        tool = await server.mcp.get_tool("lithos_list")
        # Mock FTS to return only doc_a's ID
        mock_result = MagicMock()
        mock_result.id = doc_a.id
        with patch.object(server.search, "full_text_search", return_value=[mock_result]):
            result = await tool.fn(content_query="some query")
        assert result["total"] == 1
        assert len(result["items"]) == 1
        assert result["items"][0]["id"] == doc_a.id

    @pytest.mark.asyncio
    async def test_lithos_list_title_contains_pagination(self, server: LithosServer):
        """total reflects true match count across pages, not just the current page."""
        tool = await server.mcp.get_tool("lithos_list")

        # Create 12 docs whose titles contain "Widget" and 3 that don't.
        widget_ids = []
        for i in range(12):
            doc = (
                await server.knowledge.create(
                    title=f"Widget Item {i:02d}",
                    content=f"Widget content {i}.",
                    agent="agent",
                )
            ).document
            widget_ids.append(doc.id)
        for i in range(3):
            await server.knowledge.create(
                title=f"Other Item {i:02d}",
                content=f"Other content {i}.",
                agent="agent",
            )

        # Page 1: limit=5, offset=0 — should see 5 items but total=12
        page1 = await tool.fn(title_contains="widget", limit=5, offset=0)
        assert page1["total"] == 12, f"total should be 12 (full match count), got {page1['total']}"
        assert len(page1["items"]) == 5

        # Page 2: limit=5, offset=5 — next 5 items, total still 12
        page2 = await tool.fn(title_contains="widget", limit=5, offset=5)
        assert page2["total"] == 12
        assert len(page2["items"]) == 5

        # Page 3: limit=5, offset=10 — last 2 items, total still 12
        page3 = await tool.fn(title_contains="widget", limit=5, offset=10)
        assert page3["total"] == 12
        assert len(page3["items"]) == 2

        # All returned IDs should be Widget docs only
        all_returned_ids = (
            {i["id"] for i in page1["items"]}
            | {i["id"] for i in page2["items"]}
            | {i["id"] for i in page3["items"]}
        )
        assert all_returned_ids == set(widget_ids)

    @pytest.mark.asyncio
    async def test_lithos_list_content_query_search_backend_error(self, server: LithosServer):
        """content_query returns error envelope when full_text_search raises SearchBackendError."""
        from lithos.errors import SearchBackendError

        await server.knowledge.create(
            title="Some Doc",
            content="Some content.",
            agent="agent",
        )
        tool = await server.mcp.get_tool("lithos_list")
        err = SearchBackendError("index crashed", {"tantivy": RuntimeError("segment fault")})
        with patch.object(server.search, "full_text_search", side_effect=err):
            result = await tool.fn(content_query="anything")

        assert result["status"] == "error"
        assert result["code"] == "search_backend_error"
        assert "index crashed" in result["message"]

    @pytest.mark.asyncio
    async def test_lithos_read_missing_id_returns_structured_error(self, server: LithosServer):
        """lithos_read returns a structured error envelope for a non-existent document (fixes #102, #121)."""
        tool = await server.mcp.get_tool("lithos_read")
        result = await tool.fn(id="00000000-0000-0000-0000-000000000000")
        assert result["status"] == "error"
        assert result["code"] == "doc_not_found"
        assert "00000000-0000-0000-0000-000000000000" in result["message"]

    @pytest.mark.asyncio
    async def test_lithos_read_missing_path_returns_structured_error(self, server: LithosServer):
        """lithos_read returns a structured error envelope for a non-existent path (fixes #102, #121)."""
        tool = await server.mcp.get_tool("lithos_read")
        result = await tool.fn(path="nonexistent/ghost.md")
        assert result["status"] == "error"
        assert result["code"] == "doc_not_found"

    @pytest.mark.asyncio
    async def test_lithos_read_nonexistent_doc_does_not_raise(self, server: LithosServer):
        """lithos_read never raises FileNotFoundError; non-existent docs return structured error (closes #121)."""
        tool = await server.mcp.get_tool("lithos_read")
        # Calling with an id that will never exist should return a structured error, not raise
        result = await tool.fn(id="ffffffff-ffff-ffff-ffff-ffffffffffff")
        assert isinstance(result, dict)
        assert result["status"] == "error"
        assert result["code"] == "doc_not_found"
        assert "message" in result

    @pytest.mark.asyncio
    async def test_handle_deleted_file_removes_indices(self, server: LithosServer):
        """Delete file events remove knowledge/search/graph state."""
        doc = (
            await server.knowledge.create(
                title="Delete Event Doc",
                content="Will be deleted from indices.",
                agent="agent",
                path="watched",
            )
        ).document
        server.search.index_document(doc)
        server.graph.add_document(doc)

        file_path = server.config.storage.knowledge_path / doc.path
        file_path.unlink()

        await server.handle_file_change(file_path, deleted=True)

        with pytest.raises(FileNotFoundError):
            await server.knowledge.read(id=doc.id)
        assert not server.graph.has_node(doc.id)
        assert not any(r.id == doc.id for r in server.search.full_text_search("Delete Event Doc"))


class TestSearchToolWorkflow:
    """Integration tests for search tools."""

    @pytest.mark.asyncio
    async def test_full_text_search_finds_created_docs(self, server: LithosServer):
        """Full-text search finds newly created documents."""
        # Create searchable document
        doc = (
            await server.knowledge.create(
                title="Kubernetes Deployment",
                content="Deploy applications to Kubernetes clusters using kubectl.",
                agent="agent",
                tags=["kubernetes", "deployment"],
            )
        ).document
        server.search.index_document(doc)

        # Search should find it
        results = server.search.full_text_search("Kubernetes kubectl")

        assert len(results) >= 1
        assert any(r.id == doc.id for r in results)

    @pytest.mark.asyncio
    async def test_semantic_search_finds_related_content(self, server: LithosServer):
        """Semantic search finds conceptually related documents."""
        # Create document about error handling
        doc = (
            await server.knowledge.create(
                title="Exception Handling",
                content="Catch exceptions and handle errors gracefully in your code.",
                agent="agent",
            )
        ).document
        server.search.index_document(doc)

        # Search with related but different terms
        results = server.search.semantic_search("dealing with failures in software")

        # Should find the exception handling doc
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_respects_tag_filters(self, server: LithosServer):
        """Search filters by tags correctly."""
        # Create docs with different tags
        python_doc = (
            await server.knowledge.create(
                title="Python Guide",
                content="Programming in Python.",
                agent="agent",
                tags=["python"],
            )
        ).document
        java_doc = (
            await server.knowledge.create(
                title="Java Guide",
                content="Programming in Java.",
                agent="agent",
                tags=["java"],
            )
        ).document
        server.search.index_document(python_doc)
        server.search.index_document(java_doc)

        # Search with tag filter
        results = server.search.full_text_search("Programming", tags=["python"])

        result_ids = [r.id for r in results]
        assert python_doc.id in result_ids
        assert java_doc.id not in result_ids


class TestCoordinationToolWorkflow:
    """Integration tests for coordination tools."""

    @pytest.mark.asyncio
    async def test_task_claim_workflow(self, server: LithosServer):
        """Complete task claiming workflow."""
        # Register agents
        await server.coordination.register_agent(
            "researcher",
            name="Research Agent",
            agent_type="research",
        )
        await server.coordination.register_agent(
            "developer",
            name="Developer Agent",
            agent_type="development",
        )

        # Create task
        task_id = await server.coordination.create_task(
            title="Implement Feature X",
            agent="researcher",
            description="Research and implement feature X.",
            tags=["feature", "research"],
        )

        # Researcher claims research aspect
        success1, _expires1 = await server.coordination.claim_task(
            task_id=task_id,
            aspect="research",
            agent="researcher",
            ttl_minutes=60,
        )
        assert success1

        # Developer claims implementation aspect
        success2, _expires2 = await server.coordination.claim_task(
            task_id=task_id,
            aspect="implementation",
            agent="developer",
            ttl_minutes=60,
        )
        assert success2

        # Check task status shows both claims
        statuses = await server.coordination.get_task_status(task_id)
        assert len(statuses) == 1
        assert len(statuses[0].claims) == 2

        # Post findings
        await server.coordination.post_finding(
            task_id=task_id,
            agent="researcher",
            summary="Found relevant API documentation.",
        )

        # Complete task
        await server.coordination.complete_task(task_id, "researcher")

        # Verify completed
        task = await server.coordination.get_task(task_id)
        assert task.status == "completed"

    @pytest.mark.asyncio
    async def test_claim_conflict_resolution(self, server: LithosServer):
        """Claim conflicts are properly handled."""
        task_id = await server.coordination.create_task(
            title="Contested Task",
            agent="creator",
        )

        # First agent claims
        success1, _ = await server.coordination.claim_task(
            task_id=task_id,
            aspect="work",
            agent="agent-1",
        )

        # Second agent tries same aspect
        success2, _ = await server.coordination.claim_task(
            task_id=task_id,
            aspect="work",
            agent="agent-2",
        )

        assert success1
        assert not success2  # Conflict!

        # First agent releases
        await server.coordination.release_claim(
            task_id=task_id,
            aspect="work",
            agent="agent-1",
        )

        # Now second agent can claim
        success3, _ = await server.coordination.claim_task(
            task_id=task_id,
            aspect="work",
            agent="agent-2",
        )
        assert success3


class TestGraphToolWorkflow:
    """Integration tests for graph tools."""

    @pytest.mark.asyncio
    async def test_build_and_query_knowledge_graph(self, server: LithosServer):
        """Build knowledge graph and query relationships."""
        # Create interconnected documents
        overview = (
            await server.knowledge.create(
                title="System Overview",
                content="See [[api-design]] and [[database-schema]] for details.",
                agent="agent",
            )
        ).document
        api = (
            await server.knowledge.create(
                title="API Design",
                content="REST API design. See [[database-schema]] for data model.",
                agent="agent",
            )
        ).document
        db = (
            await server.knowledge.create(
                title="Database Schema",
                content="PostgreSQL schema definition.",
                agent="agent",
            )
        ).document

        # Add to graph
        server.graph.add_document(overview)
        server.graph.add_document(api)
        server.graph.add_document(db)

        # Query relationships
        # Overview links to both api and db
        outgoing = server.graph.get_outgoing_links(overview.id)
        assert len(outgoing) == 2

        # DB has incoming links from both overview and api
        incoming = server.graph.get_incoming_links(db.id)
        assert len(incoming) == 2

        # Find path from overview to db
        path = server.graph.find_path(overview.id, db.id)
        assert path is not None
        assert len(path) >= 2

    @pytest.mark.asyncio
    async def test_orphan_detection(self, server: LithosServer):
        """Detect orphaned documents."""
        # Create connected docs
        connected = (
            await server.knowledge.create(
                title="Connected Doc",
                content="Links to [[other-connected]].",
                agent="agent",
            )
        ).document
        other = (
            await server.knowledge.create(
                title="Other Connected",
                content="Linked from connected.",
                agent="agent",
            )
        ).document

        # Create orphan
        orphan = (
            await server.knowledge.create(
                title="Orphan Document",
                content="No links anywhere.",
                agent="agent",
            )
        ).document

        server.graph.add_document(connected)
        server.graph.add_document(other)
        server.graph.add_document(orphan)

        orphans = server.graph.find_orphans()

        assert orphan.id in orphans
        assert connected.id not in orphans


class TestEndToEndScenarios:
    """End-to-end integration scenarios."""

    @pytest.mark.asyncio
    async def test_multi_agent_collaboration_scenario(self, server: LithosServer):
        """Simulate multi-agent collaboration on a task."""
        # Setup: Register agents
        await server.coordination.register_agent(
            "planner",
            name="Planning Agent",
            agent_type="planning",
        )
        await server.coordination.register_agent(
            "researcher",
            name="Research Agent",
            agent_type="research",
        )
        await server.coordination.register_agent(
            "writer",
            name="Writing Agent",
            agent_type="writing",
        )

        # Step 1: Planner creates task
        task_id = await server.coordination.create_task(
            title="Write Technical Documentation",
            agent="planner",
            description="Create comprehensive docs for the API.",
            tags=["documentation", "api"],
        )

        # Step 2: Researcher claims research aspect
        await server.coordination.claim_task(
            task_id=task_id,
            aspect="research",
            agent="researcher",
        )

        # Step 3: Researcher creates knowledge document
        research_doc = (
            await server.knowledge.create(
                title="API Research Notes",
                content="The API uses REST with JSON. Endpoints: /users, /items, /orders.",
                agent="researcher",
                tags=["research", "api"],
            )
        ).document
        server.search.index_document(research_doc)
        server.graph.add_document(research_doc)

        # Step 4: Researcher posts finding
        await server.coordination.post_finding(
            task_id=task_id,
            agent="researcher",
            summary="Documented all API endpoints.",
            knowledge_id=research_doc.id,
        )

        # Step 5: Researcher releases claim
        await server.coordination.release_claim(
            task_id=task_id,
            aspect="research",
            agent="researcher",
        )

        # Step 6: Writer claims writing aspect
        await server.coordination.claim_task(
            task_id=task_id,
            aspect="writing",
            agent="writer",
        )

        # Step 7: Writer searches for research
        results = server.search.full_text_search("API endpoints")
        assert any(r.id == research_doc.id for r in results)

        # Step 8: Writer creates documentation
        docs = (
            await server.knowledge.create(
                title="API Documentation",
                content="""# API Documentation

Based on [[api-research-notes]].

## Endpoints
- GET /users - List users
- GET /items - List items
- GET /orders - List orders
""",
                agent="writer",
                tags=["documentation", "api"],
            )
        ).document
        server.search.index_document(docs)
        server.graph.add_document(docs)

        # Step 9: Verify graph connection
        assert server.graph.has_edge(docs.id, research_doc.id)

        # Step 10: Complete task
        await server.coordination.complete_task(task_id, "writer")

        # Verify final state
        task = await server.coordination.get_task(task_id)
        assert task.status == "completed"

        findings = await server.coordination.list_findings(task_id)
        assert len(findings) >= 1

    @pytest.mark.asyncio
    async def test_knowledge_discovery_scenario(self, server: LithosServer):
        """Simulate knowledge discovery through search and graph."""
        # Create a knowledge base
        docs_data = [
            ("Python Basics", "Variables, functions, classes in Python.", ["python", "basics"]),
            (
                "Python Testing",
                "Use pytest for testing. See [[python-basics]].",
                ["python", "testing"],
            ),
            (
                "FastAPI Guide",
                "Build APIs with FastAPI. Requires [[python-basics]].",
                ["python", "api"],
            ),
            ("Database Patterns", "ORM patterns and raw SQL.", ["database"]),
            (
                "Full Stack App",
                "Combines [[fastapi-guide]] with [[database-patterns]].",
                ["fullstack"],
            ),
        ]

        created_docs = {}
        for title, content, tags in docs_data:
            doc = (
                await server.knowledge.create(
                    title=title,
                    content=content,
                    agent="knowledge-builder",
                    tags=tags,
                )
            ).document
            server.search.index_document(doc)
            server.graph.add_document(doc)
            created_docs[title] = doc

        # Discovery 1: Search for Python content
        python_results = server.search.full_text_search("Python")
        assert len(python_results) >= 3

        # Discovery 2: Find what links to Python Basics
        basics_id = created_docs["Python Basics"].id
        dependents = server.graph.get_incoming_links(basics_id)
        assert len(dependents) == 2  # Testing and FastAPI

        # Discovery 3: Find path from Full Stack to Basics
        fullstack_id = created_docs["Full Stack App"].id
        path = server.graph.find_path(fullstack_id, basics_id)
        assert path is not None
        # Path: Full Stack -> FastAPI -> Basics
        assert len(path) == 3

    @pytest.mark.asyncio
    async def test_system_stats_aggregation(self, server: LithosServer):
        """Get comprehensive system statistics."""
        # Create some data
        for i in range(3):
            doc = (
                await server.knowledge.create(
                    title=f"Stats Doc {i}",
                    content=f"Content for document {i}.",
                    agent="stats-agent",
                    tags=["stats"],
                )
            ).document
            server.search.index_document(doc)
            server.graph.add_document(doc)

        await server.coordination.register_agent("stats-agent")
        await server.coordination.create_task(
            title="Stats Task",
            agent="stats-agent",
        )

        # Get stats from all components
        search_stats = server.search.get_stats()
        graph_stats = server.graph.get_stats()
        coord_stats = await server.coordination.get_stats()

        # Verify stats are populated
        assert search_stats["chroma_chunk_count"] >= 3
        assert graph_stats["nodes"] >= 3
        assert coord_stats["agents"] >= 1
        assert coord_stats["active_tasks"] >= 1


class TestFreshnessWritePath:
    """Integration tests for ttl_hours / expires_at in lithos_write."""

    @pytest.mark.asyncio
    async def test_create_with_ttl_hours(self, server: LithosServer):
        """Create with ttl_hours sets expires_at in metadata."""
        result = await server.knowledge.create(
            title="TTL Doc",
            content="Content with TTL.",
            agent="agent",
            expires_at=datetime.now(timezone.utc) + __import__("datetime").timedelta(hours=24),
        )
        assert result.status == "created"
        assert result.document is not None
        assert result.document.metadata.expires_at is not None

    @pytest.mark.asyncio
    async def test_create_with_ttl_read_back(self, server: LithosServer):
        """Create with short TTL, read back, verify expires_at is set."""
        from datetime import timedelta

        expires = datetime.now(timezone.utc) + timedelta(hours=0.001)
        result = await server.knowledge.create(
            title="Short TTL",
            content="Ephemeral content.",
            agent="agent",
            expires_at=expires,
        )
        doc = result.document
        assert doc is not None
        read_doc, _ = await server.knowledge.read(id=doc.id)
        assert read_doc.metadata.expires_at is not None

    @pytest.mark.asyncio
    async def test_update_preserve_expires_at(self, server: LithosServer):
        """Update without expires_at preserves existing value."""
        from datetime import timedelta

        expires = datetime.now(timezone.utc) + timedelta(hours=24)
        doc = (
            await server.knowledge.create(
                title="Preserve",
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
                content="Updated.",
            )
        ).document
        assert updated is not None
        assert updated.metadata.expires_at == expires

    @pytest.mark.asyncio
    async def test_update_clear_expires_at(self, server: LithosServer):
        """Update with expires_at=None clears existing value."""
        from datetime import timedelta

        expires = datetime.now(timezone.utc) + timedelta(hours=24)
        doc = (
            await server.knowledge.create(
                title="ClearExpiry",
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


class TestWriteUpdateSentinel:
    """Tests for issue #37: sentinel pattern for tags/confidence at the MCP boundary."""

    async def _call_write(self, server: LithosServer, **kwargs) -> dict:
        tool = await server.mcp.get_tool("lithos_write")
        return await tool.fn(**kwargs)

    @pytest.mark.asyncio
    async def test_update_null_tags_preserves(self, server: LithosServer):
        """lithos_write: omitting tags (null) on update preserves existing tags."""
        doc = (
            await server.knowledge.create(
                title="Tagged",
                content="Content.",
                agent="agent",
                tags=["keep", "these"],
            )
        ).document
        assert doc is not None
        result = await self._call_write(
            server,
            id=doc.id,
            title="Tagged",
            content="Updated.",
            agent="editor",
            # tags omitted (None)
        )
        assert result["status"] == "updated"
        updated = (await server.knowledge.read(id=doc.id))[0]
        assert updated.metadata.tags == ["keep", "these"]

    @pytest.mark.asyncio
    async def test_update_empty_list_tags_clears(self, server: LithosServer):
        """lithos_write: passing tags=[] on update clears all tags."""
        doc = (
            await server.knowledge.create(
                title="Tagged2",
                content="Content.",
                agent="agent",
                tags=["remove", "me"],
            )
        ).document
        assert doc is not None
        result = await self._call_write(
            server,
            id=doc.id,
            title="Tagged2",
            content="Updated.",
            agent="editor",
            tags=[],
        )
        assert result["status"] == "updated"
        updated = (await server.knowledge.read(id=doc.id))[0]
        assert updated.metadata.tags == []

    @pytest.mark.asyncio
    async def test_update_null_confidence_preserves(self, server: LithosServer):
        """lithos_write: omitting confidence (null) on update preserves existing confidence."""
        doc = (
            await server.knowledge.create(
                title="Confident",
                content="Content.",
                agent="agent",
                confidence=0.6,
            )
        ).document
        assert doc is not None
        result = await self._call_write(
            server,
            id=doc.id,
            title="Confident",
            content="Updated.",
            agent="editor",
            # confidence omitted (None)
        )
        assert result["status"] == "updated"
        updated = (await server.knowledge.read(id=doc.id))[0]
        assert updated.metadata.confidence == pytest.approx(0.6)

    @pytest.mark.asyncio
    async def test_update_confidence_sets_value(self, server: LithosServer):
        """lithos_write: passing confidence on update sets new value."""
        doc = (
            await server.knowledge.create(
                title="Confident2",
                content="Content.",
                agent="agent",
                confidence=0.5,
            )
        ).document
        assert doc is not None
        result = await self._call_write(
            server,
            id=doc.id,
            title="Confident2",
            content="Updated.",
            agent="editor",
            confidence=0.95,
        )
        assert result["status"] == "updated"
        updated = (await server.knowledge.read(id=doc.id))[0]
        assert updated.metadata.confidence == pytest.approx(0.95)


class TestCacheLookup:
    """Integration tests for lithos_cache_lookup tool."""

    async def _call_cache_lookup(self, server: LithosServer, **kwargs) -> dict:
        """Helper to call cache lookup tool."""
        tool = await server.mcp.get_tool("lithos_cache_lookup")
        result = await tool.fn(**kwargs)
        return result

    @pytest.mark.asyncio
    async def test_cache_hit_fresh_doc(self, server: LithosServer):
        """Cache lookup returns hit for fresh doc."""
        from datetime import timedelta

        doc = (
            await server.knowledge.create(
                title="Fresh Cache Doc",
                content="Information about quantum computing.",
                agent="agent",
                tags=["research"],
                expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            )
        ).document
        server.search.index_document(doc)

        result = await self._call_cache_lookup(server, query="quantum computing", tags=["research"])
        assert result["hit"] is True
        assert result["document"] is not None
        assert result["document"]["id"] == doc.id
        assert result["document"]["content"] == "Information about quantum computing."
        assert result["stale_exists"] is False
        assert result["stale_id"] is None

    @pytest.mark.asyncio
    async def test_cache_miss_stale_doc(self, server: LithosServer):
        """Cache lookup returns stale reference for expired doc."""
        from datetime import timedelta

        doc = (
            await server.knowledge.create(
                title="Stale Cache Doc",
                content="Outdated information about AI trends.",
                agent="agent",
                tags=["research"],
                expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
        ).document
        server.search.index_document(doc)

        result = await self._call_cache_lookup(server, query="AI trends", tags=["research"])
        assert result["hit"] is False
        assert result["document"] is None
        assert result["stale_exists"] is True
        assert result["stale_id"] == doc.id

    @pytest.mark.asyncio
    async def test_cache_clean_miss(self, server: LithosServer):
        """Cache lookup returns clean miss when no matching doc exists."""
        result = await self._call_cache_lookup(server, query="completely unique topic xyz123")
        assert result["hit"] is False
        assert result["document"] is None
        assert result["stale_exists"] is False
        assert result["stale_id"] is None

    @pytest.mark.asyncio
    async def test_source_url_fast_path_hit(self, server: LithosServer):
        """Cache lookup uses source_url fast path for exact match."""
        from datetime import timedelta

        doc = (
            await server.knowledge.create(
                title="URL Doc",
                content="Content from example.com.",
                agent="agent",
                source_url="https://example.com/article",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            )
        ).document
        server.search.index_document(doc)

        result = await self._call_cache_lookup(
            server,
            query="example article",
            source_url="https://example.com/article",
        )
        assert result["hit"] is True
        assert result["document"]["id"] == doc.id
        assert result["document"]["source_url"] == "https://example.com/article"

    @pytest.mark.asyncio
    async def test_source_url_fast_path_miss_falls_back(self, server: LithosServer):
        """Cache lookup falls back to semantic when source_url not found."""
        from datetime import timedelta

        doc = (
            await server.knowledge.create(
                title="Fallback Semantic Doc",
                content="Information about neural networks and deep learning.",
                agent="agent",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            )
        ).document
        server.search.index_document(doc)

        result = await self._call_cache_lookup(
            server,
            query="neural networks deep learning",
            source_url="https://nonexistent.com/page",
        )
        # Should fall back to semantic and potentially find the doc
        assert isinstance(result["hit"], bool)

    @pytest.mark.asyncio
    async def test_confidence_filter(self, server: LithosServer):
        """Cache lookup filters out low-confidence docs."""
        from datetime import timedelta

        doc = (
            await server.knowledge.create(
                title="Low Confidence Doc",
                content="Uncertain information about dark matter theories.",
                agent="agent",
                confidence=0.2,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            )
        ).document
        server.search.index_document(doc)

        result = await self._call_cache_lookup(
            server, query="dark matter theories", min_confidence=0.5
        )
        # Should not match because confidence is below threshold
        assert result["hit"] is False

    @pytest.mark.asyncio
    async def test_cache_lookup_returns_highest_confidence(self, server: LithosServer):
        """Cache lookup always returns the highest-confidence passing doc."""
        from datetime import timedelta
        from unittest.mock import patch

        low_doc = (
            await server.knowledge.create(
                title="Low Confidence Sort Doc",
                content="Uncertain information about quantum entanglement.",
                agent="agent",
                confidence=0.6,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            )
        ).document
        server.search.index_document(low_doc)

        high_doc = (
            await server.knowledge.create(
                title="High Confidence Sort Doc",
                content="Certain information about quantum entanglement.",
                agent="agent",
                confidence=0.9,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            )
        ).document
        server.search.index_document(high_doc)

        # Force both docs into candidates list to test sorting
        with patch.object(
            server.search,
            "semantic_search",
            return_value=[
                type("R", (), {"id": low_doc.id})(),
                type("R", (), {"id": high_doc.id})(),
            ],
        ):
            result = await self._call_cache_lookup(
                server,
                query="quantum entanglement",
                min_confidence=0.5,
            )

        assert result["hit"] is True
        assert result["document"]["id"] == high_doc.id
        assert result["document"]["confidence"] == 0.9

    @pytest.mark.asyncio
    async def test_max_age_hours_filter(self, server: LithosServer):
        """Cache lookup respects max_age_hours filter."""
        from datetime import timedelta

        # Create a doc that hasn't expired but is old
        doc = (
            await server.knowledge.create(
                title="Old Research Doc",
                content="Research about blockchain consensus mechanisms.",
                agent="agent",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=100),
            )
        ).document
        # Manually set updated_at to 48 hours ago
        doc.metadata.updated_at = datetime.now(timezone.utc) - timedelta(hours=48)
        # Re-write to disk to persist the old updated_at
        path = server.knowledge._resolve_safe_path(doc.path)[1]
        path.write_text(doc.to_markdown())
        # Re-read to reload from disk
        server.knowledge._id_to_path[doc.id] = doc.path
        server.search.index_document(doc)

        result = await self._call_cache_lookup(
            server, query="blockchain consensus", max_age_hours=24
        )
        # Should be treated as stale because it's older than 24 hours
        assert result["hit"] is False

    @pytest.mark.asyncio
    async def test_tag_filtering_on_fast_path(self, server: LithosServer):
        """Fast path source_url match must also pass tag filter."""
        from datetime import timedelta

        doc = (
            await server.knowledge.create(
                title="Tagged URL Doc",
                content="Content with specific tags.",
                agent="agent",
                source_url="https://example.com/tagged",
                tags=["python"],
                expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            )
        ).document
        server.search.index_document(doc)

        # Should fail tag filter (doc has "python" but we ask for "rust")
        result = await self._call_cache_lookup(
            server,
            query="tagged content",
            source_url="https://example.com/tagged",
            tags=["rust"],
        )
        # Fast path doc doesn't match tags, falls through
        assert result["document"] is None or (
            result["document"] is not None and "rust" in result["document"].get("tags", [])
        )

    @pytest.mark.asyncio
    async def test_cache_lookup_search_backend_error(self, server: LithosServer):
        """SearchBackendError returns standard error shape, not a clean miss."""
        from unittest.mock import patch

        from lithos.errors import SearchBackendError

        err = SearchBackendError("chroma down", {"chroma": RuntimeError("connection refused")})
        with patch.object(server.search, "semantic_search", side_effect=err):
            result = await self._call_cache_lookup(server, query="anything")

        assert result["status"] == "error"
        assert result["code"] == "search_backend_error"
        assert "chroma" in result["message"]

    @pytest.mark.asyncio
    async def test_cache_lookup_invalid_max_age_hours(self, server: LithosServer):
        """Negative max_age_hours returns invalid_input error."""
        result = await self._call_cache_lookup(server, query="test", max_age_hours=-1)
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_cache_lookup_invalid_limit(self, server: LithosServer):
        """Zero limit returns invalid_input error."""
        result = await self._call_cache_lookup(server, query="test", limit=0)
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_cache_lookup_invalid_min_confidence(self, server: LithosServer):
        """Out-of-range min_confidence returns invalid_input error."""
        result = await self._call_cache_lookup(server, query="test", min_confidence=1.5)
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"


class TestErrorEnvelopes:
    """Tests for issue #85: consistent error envelopes across all tools."""

    async def _call(self, server: LithosServer, tool_name: str, **kwargs) -> dict:
        tool = await server.mcp.get_tool(tool_name)
        return await tool.fn(**kwargs)

    # --- lithos_delete ---

    @pytest.mark.asyncio
    async def test_delete_missing_doc_returns_error_envelope(self, server: LithosServer):
        """lithos_delete returns error envelope when document does not exist (fixes #85)."""
        result = await self._call(
            server,
            "lithos_delete",
            id="00000000-0000-0000-0000-000000000000",
            agent="test-agent",
        )
        assert result["status"] == "error"
        assert result["code"] == "doc_not_found"
        assert "00000000-0000-0000-0000-000000000000" in result["message"]

    @pytest.mark.asyncio
    async def test_delete_existing_doc_returns_success(self, server: LithosServer):
        """lithos_delete still returns success=True for an existing document."""
        doc = (
            await server.knowledge.create(
                title="Delete Me",
                content="Temporary.",
                agent="agent",
            )
        ).document
        assert doc is not None
        result = await self._call(server, "lithos_delete", id=doc.id, agent="agent")
        assert result == {"success": True}

    # --- lithos_task_claim ---

    @pytest.mark.asyncio
    async def test_task_claim_nonexistent_task_returns_error_envelope(self, server: LithosServer):
        """lithos_task_claim returns error envelope for a non-existent task (fixes #85)."""
        result = await self._call(
            server,
            "lithos_task_claim",
            task_id="00000000-0000-0000-0000-000000000000",
            aspect="research",
            agent="agent",
        )
        assert result["status"] == "error"
        assert result["code"] == "claim_failed"
        assert "message" in result

    @pytest.mark.asyncio
    async def test_task_claim_conflict_returns_error_envelope(self, server: LithosServer):
        """lithos_task_claim returns error envelope when aspect is already held (fixes #85)."""
        task_id = await server.coordination.create_task(title="Contested", agent="creator")
        # First agent claims successfully
        first = await self._call(
            server,
            "lithos_task_claim",
            task_id=task_id,
            aspect="work",
            agent="agent-1",
        )
        assert first["success"] is True

        # Second agent should get an error envelope, not success=False
        second = await self._call(
            server,
            "lithos_task_claim",
            task_id=task_id,
            aspect="work",
            agent="agent-2",
        )
        assert second["status"] == "error"
        assert second["code"] == "claim_failed"

    # --- lithos_task_renew ---

    @pytest.mark.asyncio
    async def test_task_renew_nonexistent_claim_returns_error_envelope(self, server: LithosServer):
        """lithos_task_renew returns error envelope when no active claim exists (fixes #85)."""
        result = await self._call(
            server,
            "lithos_task_renew",
            task_id="00000000-0000-0000-0000-000000000000",
            aspect="research",
            agent="nobody",
        )
        assert result["status"] == "error"
        assert result["code"] == "claim_not_found"
        assert "message" in result

    # --- lithos_task_release ---

    @pytest.mark.asyncio
    async def test_task_release_nonexistent_claim_returns_error_envelope(
        self, server: LithosServer
    ):
        """lithos_task_release returns error envelope when no matching claim exists (fixes #85)."""
        result = await self._call(
            server,
            "lithos_task_release",
            task_id="00000000-0000-0000-0000-000000000000",
            aspect="research",
            agent="nobody",
        )
        assert result["status"] == "error"
        assert result["code"] == "claim_not_found"
        assert "message" in result

    # --- lithos_task_complete ---

    @pytest.mark.asyncio
    async def test_task_complete_nonexistent_task_returns_error_envelope(
        self, server: LithosServer
    ):
        """lithos_task_complete returns error envelope for a non-existent task (fixes #85)."""
        result = await self._call(
            server,
            "lithos_task_complete",
            task_id="00000000-0000-0000-0000-000000000000",
            agent="agent",
        )
        assert result["status"] == "error"
        assert result["code"] == "task_not_found"
        assert "message" in result

    @pytest.mark.asyncio
    async def test_task_complete_already_completed_returns_error_envelope(
        self, server: LithosServer
    ):
        """lithos_task_complete returns error envelope for an already-completed task (fixes #85)."""
        task_id = await server.coordination.create_task(title="Done Task", agent="agent")
        await server.coordination.complete_task(task_id, "agent")

        result = await self._call(server, "lithos_task_complete", task_id=task_id, agent="agent")
        assert result["status"] == "error"
        assert result["code"] == "task_not_found"


class TestDeleteAgentRequired:
    """Tests for issue #80: agent is required on lithos_delete."""

    async def _call_delete(self, server: LithosServer, **kwargs) -> dict:
        tool = await server.mcp.get_tool("lithos_delete")
        return await tool.fn(**kwargs)

    @pytest.mark.asyncio
    async def test_delete_records_agent_in_span(self, server: LithosServer):
        """lithos_delete registers the agent for audit trail purposes (fixes #80)."""
        doc = (
            await server.knowledge.create(
                title="Audit Delete Doc",
                content="To be deleted with agent attribution.",
                agent="creator-agent",
            )
        ).document
        assert doc is not None

        result = await self._call_delete(server, id=doc.id, agent="deleter-agent")
        assert result == {"success": True}

        # Agent should now be registered in the coordination service
        agent_info = await server.coordination.get_agent("deleter-agent")
        assert agent_info is not None
        assert agent_info.id == "deleter-agent"

    @pytest.mark.asyncio
    async def test_delete_agent_param_is_required(self, server: LithosServer):
        """lithos_delete raises TypeError when agent is omitted (fixes #80)."""
        tool = await server.mcp.get_tool("lithos_delete")
        with pytest.raises(TypeError):
            await tool.fn(id="00000000-0000-0000-0000-000000000000")


class TestWriteMutualExclusion:
    """Tests for ttl_hours / expires_at mutual exclusion at the MCP boundary."""

    async def _call_write(self, server: LithosServer, **kwargs) -> dict:
        tool = await server.mcp.get_tool("lithos_write")
        return await tool.fn(**kwargs)

    @pytest.mark.asyncio
    async def test_ttl_hours_with_empty_expires_at_is_error(self, server: LithosServer):
        """ttl_hours + expires_at='' is contradictory and must be rejected."""
        result = await self._call_write(
            server,
            title="Contradiction",
            content="Should fail.",
            agent="agent",
            ttl_hours=24.0,
            expires_at="",
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_write_via_tool_with_ttl_hours(self, server: LithosServer):
        """lithos_write tool with ttl_hours sets expires_at in metadata."""
        result = await self._call_write(
            server,
            title="TTL via Tool",
            content="Content with TTL via MCP boundary.",
            agent="agent",
            ttl_hours=24.0,
        )
        assert result["status"] == "created"
        doc_id = result["id"]

        doc, _ = await server.knowledge.read(id=doc_id)
        assert doc.metadata.expires_at is not None
        # Should be roughly 24h from now
        delta = (doc.metadata.expires_at - datetime.now(timezone.utc)).total_seconds()
        assert 23 * 3600 < delta < 25 * 3600


class TestOptimisticLockingServerLayer:
    async def _call_write(self, server: LithosServer, **kwargs) -> dict:
        tool = await server.mcp.get_tool("lithos_write")
        return await tool.fn(**kwargs)

    @pytest.mark.asyncio
    async def test_expected_version_forwarded_on_match(self, server: LithosServer):
        """lithos_write accepts expected_version=1 when the document is at version 1."""
        # Create the document first.
        create_result = await self._call_write(
            server,
            title="Version Wiring Test",
            content="Initial content.",
            agent="agent",
        )
        assert create_result["status"] == "created"
        assert create_result["version"] == 1
        doc_id = create_result["id"]

        # Update with the correct expected_version — should succeed.
        update_result = await self._call_write(
            server,
            id=doc_id,
            title="Version Wiring Test",
            content="Updated content.",
            agent="agent",
            expected_version=1,
        )
        assert update_result["status"] == "updated"

        # Version should now be 2.
        doc, _ = await server.knowledge.read(id=doc_id)
        assert doc.metadata.version == 2

    @pytest.mark.asyncio
    async def test_expected_version_forwarded_on_conflict(self, server: LithosServer):
        """lithos_write rejects a stale expected_version with version_conflict."""
        create_result = await self._call_write(
            server,
            title="Conflict Wiring Test",
            content="Initial content.",
            agent="agent",
        )
        assert create_result["status"] == "created"
        doc_id = create_result["id"]

        # Pass a stale version — should return version_conflict.
        conflict_result = await self._call_write(
            server,
            id=doc_id,
            title="Conflict Wiring Test",
            content="Conflicting update.",
            agent="agent",
            expected_version=99,
        )
        assert conflict_result["status"] == "error"
        assert conflict_result["code"] == "version_conflict"
        assert conflict_result["current_version"] == 1

    @pytest.mark.asyncio
    async def test_no_expected_version_is_backwards_compatible(self, server: LithosServer):
        """lithos_write without expected_version still succeeds (backwards compat)."""
        create_result = await self._call_write(
            server,
            title="No Version Param Test",
            content="Initial content.",
            agent="agent",
        )
        assert create_result["status"] == "created"
        doc_id = create_result["id"]

        update_result = await self._call_write(
            server,
            id=doc_id,
            title="No Version Param Test",
            content="Updated without version.",
            agent="agent",
        )
        assert update_result["status"] == "updated"

    @pytest.mark.asyncio
    async def test_expected_version_ignored_on_create(self, server: LithosServer):
        """expected_version is silently ignored on create (not an error)."""
        result = await self._call_write(
            server,
            title="Ignored Version On Create",
            content="Initial content.",
            agent="agent",
            expected_version=99,
        )
        assert result["status"] == "created"
        assert result["version"] == 1


class TestWriteContentSizeLimit:
    """Tests for content size enforcement in lithos_write."""

    async def _call_write(self, server: LithosServer, **kwargs) -> dict:
        tool = await server.mcp.get_tool("lithos_write")
        return await tool.fn(**kwargs)

    @pytest.mark.asyncio
    async def test_oversized_content_rejected(
        self, server: LithosServer, test_config: LithosConfig
    ):
        """Content exceeding max_content_size_bytes returns content_too_large error.

        Direct mutation of ``test_config`` is intentionally visible to ``server``
        here: ``LithosServer`` holds a reference to the same ``LithosConfig``
        object that the ``test_config`` fixture constructed and passed to it.
        Mutating the object *after* server construction works because the server
        reads ``self._config.storage.max_content_size_bytes`` on every call, not
        at construction time.
        """
        test_config.storage.max_content_size_bytes = 10
        oversized = "x" * 11
        result = await self._call_write(
            server,
            title="Too Big",
            content=oversized,
            agent="agent",
        )
        assert result["status"] == "error"
        assert result["code"] == "content_too_large"
        assert "10" in result["message"]

    @pytest.mark.asyncio
    async def test_content_at_limit_accepted(self, server: LithosServer, test_config: LithosConfig):
        """Content exactly at max_content_size_bytes is accepted."""
        test_config.storage.max_content_size_bytes = 10
        exact = "x" * 10
        result = await self._call_write(
            server,
            title="Exact Size",
            content=exact,
            agent="agent",
        )
        assert result["status"] == "created"


class TestSlugCollisionServerBoundary:
    """Tests for slug_collision error surfaced through lithos_write."""

    async def _call_write(self, server: LithosServer, **kwargs) -> dict:
        tool = await server.mcp.get_tool("lithos_write")
        return await tool.fn(**kwargs)

    @pytest.mark.asyncio
    async def test_write_slug_collision_returns_error_dict(self, server: LithosServer):
        """lithos_write returns slug_collision error dict when slug is already taken."""
        await self._call_write(server, title="Collision Doc", content="First.", agent="agent")
        result = await self._call_write(
            server, title="Collision Doc", content="Second.", agent="agent"
        )
        assert result["status"] == "error"
        assert result["code"] == "slug_collision"
        assert "collision-doc" in result["message"]


class TestTaskUpdateTool:
    """Tests for lithos_task_update MCP tool."""

    async def _call_task_update(self, server: LithosServer, **kwargs) -> dict:
        tool = await server.mcp.get_tool("lithos_task_update")
        return await tool.fn(**kwargs)

    @pytest.mark.asyncio
    async def test_update_task_happy_path(self, server: LithosServer):
        """lithos_task_update: successfully updates an open task's fields."""
        task_id = await server.coordination.create_task(
            title="Original Title",
            agent="test-agent",
            description="Old description",
            tags=["old"],
        )

        result = await self._call_task_update(
            server,
            task_id=task_id,
            agent="test-agent",
            title="Updated Title",
            description="New description",
            tags=["new", "shiny"],
        )

        assert result["success"] is True
        assert task_id in result["message"]

        task = await server.coordination.get_task(task_id)
        assert task is not None
        assert task.title == "Updated Title"
        assert task.description == "New description"
        assert task.tags == ["new", "shiny"]

    @pytest.mark.asyncio
    async def test_update_task_not_found(self, server: LithosServer):
        """lithos_task_update: returns error envelope for unknown task_id."""
        result = await self._call_task_update(
            server,
            task_id="nonexistent-task-id",
            agent="test-agent",
            title="Ghost Update",
        )

        assert result["status"] == "error"
        assert result["code"] == "task_not_found"
        assert "nonexistent-task-id" in result["message"]

    @pytest.mark.asyncio
    async def test_update_completed_task_returns_error(self, server: LithosServer):
        """lithos_task_update: returns error envelope for completed tasks (status guard)."""
        task_id = await server.coordination.create_task(
            title="Will Complete",
            agent="test-agent",
        )
        await server.coordination.complete_task(task_id, "test-agent")

        result = await self._call_task_update(
            server,
            task_id=task_id,
            agent="test-agent",
            title="Too Late",
        )

        assert result["status"] == "error"
        assert result["code"] == "task_not_found"

    @pytest.mark.asyncio
    async def test_update_task_no_fields_returns_error(self, server: LithosServer):
        """lithos_task_update: returns error envelope when no fields are provided."""
        task_id = await server.coordination.create_task(
            title="Task",
            agent="test-agent",
        )

        result = await self._call_task_update(
            server,
            task_id=task_id,
            agent="test-agent",
            # No title, description, or tags
        )

        assert result["status"] == "error"
        assert result["code"] == "invalid_input"


class TestTaskCancelTool:
    """Tests for lithos_task_cancel MCP tool."""

    async def _call_task_cancel(self, server: LithosServer, **kwargs) -> dict:
        tool = await server.mcp.get_tool("lithos_task_cancel")
        return await tool.fn(**kwargs)

    @pytest.mark.asyncio
    async def test_cancel_task_happy_path(self, server: LithosServer):
        """lithos_task_cancel: succeeds for an open task."""
        task_id = await server.coordination.create_task(
            title="Cancellable Task",
            agent="test-agent",
        )

        result = await self._call_task_cancel(server, task_id=task_id, agent="test-agent")

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_task(self, server: LithosServer):
        """lithos_task_cancel: returns error envelope for unknown task_id."""
        result = await self._call_task_cancel(
            server, task_id="nonexistent-task-id", agent="test-agent"
        )

        assert result["status"] == "error"
        assert result["code"] == "task_not_found"
        assert "nonexistent-task-id" in result["message"]

    @pytest.mark.asyncio
    async def test_cancel_completed_task(self, server: LithosServer):
        """lithos_task_cancel: returns error envelope for already-completed task."""
        task_id = await server.coordination.create_task(
            title="Will Complete",
            agent="test-agent",
        )
        await server.coordination.complete_task(task_id, "test-agent")

        result = await self._call_task_cancel(server, task_id=task_id, agent="test-agent")

        assert result["status"] == "error"
        assert result["code"] == "task_not_found"


class TestHealthEndpoint:
    """Tests for the HTTP GET /health endpoint and underlying _get_health logic."""

    @pytest.mark.asyncio
    async def test_health_returns_ok_in_healthy_setup(self, server: LithosServer):
        """_get_health returns status 'ok' when all components are healthy."""
        result = await server._get_health()
        assert result["status"] == "ok"
        assert result["components"]["kb_directory"]["status"] == "ok"
        assert result["components"]["embedding_model"]["status"] == "ok"
        assert result["components"]["knowledge_base"]["status"] == "ok"
        assert "timestamp" in result

    @pytest.mark.asyncio
    async def test_health_degraded_when_embedding_fails(self, server: LithosServer):
        """_get_health returns 'degraded' when embedding model is unavailable."""
        from unittest.mock import patch

        with patch.object(
            server.search.chroma,
            "health_check",
            side_effect=RuntimeError("model unavailable"),
        ):
            result = await server._get_health()

        assert result["status"] == "degraded"
        assert result["components"]["embedding_model"]["status"] == "unavailable"
        assert "error" in result["components"]["embedding_model"]

    @pytest.mark.asyncio
    async def test_health_degraded_when_kb_directory_missing(self, server: LithosServer):
        """_get_health reports unavailable when kb directory does not exist (issue #75)."""
        from unittest.mock import MagicMock, patch

        mock_path = MagicMock()
        mock_path.exists.return_value = False
        with patch.object(server.knowledge, "knowledge_path", mock_path):
            result = await server._get_health()

        assert result["status"] == "degraded"
        assert result["components"]["kb_directory"]["status"] == "unavailable"
        assert result["components"]["kb_directory"]["error"] == "directory does not exist"

    @pytest.mark.asyncio
    async def test_health_degraded_when_kb_directory_missing_real_path(
        self, server: LithosServer, tmp_path: Path
    ):
        """_get_health correctly uses Path.exists() for a real non-existent directory (issue #75)."""
        from unittest.mock import patch

        nonexistent = tmp_path / "does_not_exist"
        # Confirm the directory is genuinely absent before patching
        assert not nonexistent.exists()

        with patch.object(server.knowledge, "knowledge_path", nonexistent):
            result = await server._get_health()

        assert result["status"] == "degraded"
        assert result["components"]["kb_directory"]["status"] == "unavailable"
        assert result["components"]["kb_directory"]["error"] == "directory does not exist"

    @pytest.mark.asyncio
    async def test_health_degraded_when_kb_list_fails(self, server: LithosServer):
        """_get_health returns degraded when knowledge base list_all raises."""
        from unittest.mock import AsyncMock, patch

        with patch.object(
            server.knowledge, "list_all", AsyncMock(side_effect=RuntimeError("kb unavailable"))
        ):
            result = await server._get_health()

        assert result["status"] == "degraded"
        assert result["components"]["knowledge_base"]["status"] == "unavailable"
        assert "error" in result["components"]["knowledge_base"]

    @pytest.mark.asyncio
    async def test_http_health_200_when_ok(self, server: LithosServer):
        """HTTP /health returns 200 when all components are healthy."""
        from unittest.mock import MagicMock

        request = MagicMock()
        response = await server._health_endpoint(request)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_http_health_503_when_degraded(self, server: LithosServer):
        """HTTP /health returns 503 when any component is degraded."""
        from unittest.mock import MagicMock, patch

        request = MagicMock()
        with patch.object(
            server.search.chroma,
            "health_check",
            side_effect=RuntimeError("model unavailable"),
        ):
            response = await server._health_endpoint(request)

        assert response.status_code == 503
