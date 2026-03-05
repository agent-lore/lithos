"""Integration conformance tests focused on MCP boundary contracts."""

import asyncio
import json
from pathlib import Path
from typing import Any

import frontmatter
import pytest

from lithos.config import LithosConfig
from lithos.server import LithosServer, _FileChangeHandler

pytestmark = pytest.mark.integration


async def _call_tool(server: LithosServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call an MCP tool and return its JSON payload."""
    result = await server.mcp._call_tool_mcp(name, arguments)

    if isinstance(result, tuple):
        payload = result[1]
        if isinstance(payload, dict):
            return payload

    if hasattr(result, "content"):  # MCP CallToolResult
        content = getattr(result, "content", [])
    else:
        content = result

    if isinstance(content, list) and content:
        text = getattr(content[0], "text", None)
        if isinstance(text, str):
            return json.loads(text)

    raise AssertionError(f"Unable to decode MCP result for tool {name!r}: {result!r}")


async def _wait_for_full_text_hit(server: LithosServer, query: str, doc_id: str) -> None:
    """Wait briefly for projection consistency in search index."""
    for _ in range(20):
        payload = await _call_tool(server, "lithos_search", {"query": query, "limit": 10})
        if any(item["id"] == doc_id for item in payload["results"]):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"Document {doc_id} not found in search results for query={query!r}")


async def _wait_for_semantic_hit(
    server: LithosServer, query: str, doc_id: str, threshold: float = 0.0
) -> None:
    """Wait briefly for semantic search to find a document."""
    for _ in range(20):
        payload = await _call_tool(
            server, "lithos_semantic", {"query": query, "limit": 10, "threshold": threshold}
        )
        if any(item["id"] == doc_id for item in payload["results"]):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"Document {doc_id} not found in semantic results for query={query!r}"
    )


async def _wait_for_full_text_miss(server: LithosServer, query: str, doc_id: str) -> None:
    """Wait briefly for a document to disappear from full-text search."""
    for _ in range(20):
        payload = await _call_tool(server, "lithos_search", {"query": query, "limit": 10})
        if not any(item["id"] == doc_id for item in payload["results"]):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"Document {doc_id} still present in search results for query={query!r}"
    )


async def _wait_for_semantic_miss(
    server: LithosServer, query: str, doc_id: str, threshold: float = 0.0
) -> None:
    """Wait briefly for a document to disappear from semantic search."""
    for _ in range(20):
        payload = await _call_tool(
            server, "lithos_semantic", {"query": query, "limit": 10, "threshold": threshold}
        )
        if not any(item["id"] == doc_id for item in payload["results"]):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"Document {doc_id} still present in semantic results for query={query!r}"
    )


class TestMCPToolContracts:
    """Contract tests for MCP tool responses."""

    @pytest.mark.asyncio
    async def test_write_read_list_delete_contract(self, server: LithosServer):
        write_payload = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Conformance Doc",
                "content": "This validates MCP response shape.",
                "agent": "conformance-agent",
                "tags": ["conformance", "contract"],
                "path": "conformance",
            },
        )
        assert set(write_payload) == {"id", "path"}
        assert isinstance(write_payload["id"], str)
        assert write_payload["path"].endswith(".md")
        assert write_payload["path"].startswith("conformance/")

        doc_id = write_payload["id"]
        read_payload = await _call_tool(server, "lithos_read", {"id": doc_id})
        assert read_payload["id"] == doc_id
        assert read_payload["title"] == "Conformance Doc"
        assert isinstance(read_payload["metadata"], dict)
        assert isinstance(read_payload["links"], list)
        assert read_payload["truncated"] is False

        list_payload = await _call_tool(server, "lithos_list", {"path_prefix": "conformance"})
        assert "items" in list_payload
        assert "total" in list_payload
        assert isinstance(list_payload["items"], list)
        assert isinstance(list_payload["total"], int)
        assert any(item["id"] == doc_id for item in list_payload["items"])

        delete_payload = await _call_tool(server, "lithos_delete", {"id": doc_id})
        assert delete_payload == {"success": True}

    @pytest.mark.asyncio
    async def test_projection_consistency_after_write(self, server: LithosServer):
        write_payload = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Projection Conformance",
                "content": "Index this content for projection consistency checks.",
                "agent": "conformance-agent",
                "tags": ["projection"],
            },
        )
        doc_id = write_payload["id"]

        # Read/list should be immediately consistent with successful write.
        read_payload = await _call_tool(server, "lithos_read", {"id": doc_id})
        assert read_payload["id"] == doc_id

        list_payload = await _call_tool(server, "lithos_list", {"limit": 100})
        assert any(item["id"] == doc_id for item in list_payload["items"])

        # Search is a projection and can converge shortly after write.
        await _wait_for_full_text_hit(server, "projection consistency checks", doc_id)

    @pytest.mark.asyncio
    async def test_update_replaces_old_search_results(self, server: LithosServer):
        """After update, old content is unsearchable and new content is findable."""
        write_payload = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Search Replace Doc",
                "content": "The ancient aqueduct carried fresh water across the valley.",
                "agent": "search-agent",
            },
        )
        doc_id = write_payload["id"]
        await _wait_for_full_text_hit(server, "ancient aqueduct", doc_id)

        # Update with completely different content.
        await _call_tool(
            server,
            "lithos_write",
            {
                "id": doc_id,
                "title": "Search Replace Doc",
                "content": "The modern pipeline distributes natural gas to the district.",
                "agent": "search-agent",
            },
        )

        # New content should be findable.
        await _wait_for_full_text_hit(server, "modern pipeline", doc_id)

        # Old content should be gone from full-text search.
        old_payload = await _call_tool(
            server, "lithos_search", {"query": "ancient aqueduct", "limit": 10}
        )
        assert not any(item["id"] == doc_id for item in old_payload["results"])

        # Semantic search should reflect the new content.
        await _wait_for_semantic_hit(server, "pipeline distributes gas", doc_id)

    @pytest.mark.asyncio
    async def test_delete_removes_from_semantic_search(self, server: LithosServer):
        """Delete removes document from semantic search and leaves no orphaned chunks."""
        write_payload = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Semantic Delete Doc",
                "content": "Quantum entanglement enables instantaneous correlation between particles.",
                "agent": "semantic-agent",
            },
        )
        doc_id = write_payload["id"]
        await _wait_for_semantic_hit(server, "quantum entanglement particles", doc_id)

        initial_count = server.search.chroma.collection.count()
        assert initial_count > 0

        await _call_tool(server, "lithos_delete", {"id": doc_id})

        await _wait_for_semantic_miss(server, "quantum entanglement particles", doc_id)

        # No orphaned chunks should remain for this document.
        remaining = server.search.chroma.collection.get(where={"doc_id": doc_id})
        assert len(remaining["ids"]) == 0

    @pytest.mark.asyncio
    async def test_delete_cascade_all_subsystems(self, server: LithosServer):
        """Delete removes document from every subsystem: read, full-text, semantic, graph, slug."""
        write_payload = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Cascade Delete Target",
                "content": "Neural network architecture for deep learning classification.",
                "agent": "cascade-agent",
                "tags": ["cascade"],
            },
        )
        doc_id = write_payload["id"]
        await _wait_for_full_text_hit(server, "neural network architecture", doc_id)
        await _wait_for_semantic_hit(server, "neural network deep learning", doc_id)

        # Pre-delete: verify presence in all subsystems.
        assert server.graph.has_node(doc_id)
        assert server.knowledge.get_id_by_slug("cascade-delete-target") == doc_id
        assert doc_id in server.knowledge._id_to_path

        await _call_tool(server, "lithos_delete", {"id": doc_id})

        # Knowledge layer: read should fail.
        try:
            await _call_tool(server, "lithos_read", {"id": doc_id})
            read_failed = False
        except Exception:
            read_failed = True
        assert read_failed, "lithos_read should fail after delete"

        # Full-text search: absent.
        ft_payload = await _call_tool(
            server, "lithos_search", {"query": "neural network architecture", "limit": 10}
        )
        assert not any(item["id"] == doc_id for item in ft_payload["results"])

        # Semantic search: absent.
        sem_payload = await _call_tool(
            server, "lithos_semantic", {"query": "neural network deep learning", "limit": 10}
        )
        assert not any(item["id"] == doc_id for item in sem_payload["results"])

        # Graph: absent.
        assert not server.graph.has_node(doc_id)

        # Slug index: absent.
        assert server.knowledge.get_id_by_slug("cascade-delete-target") is None

        # Path index: absent.
        assert doc_id not in server.knowledge._id_to_path


class TestRestartPersistence:
    """Persistence tests across server restarts."""

    @pytest.mark.asyncio
    async def test_doc_and_task_survive_restart(self, test_config: LithosConfig):
        first = LithosServer(test_config)
        await first.initialize()

        write_payload = await _call_tool(
            first,
            "lithos_write",
            {
                "title": "Restart Durable Doc",
                "content": "This document should survive restart.",
                "agent": "restart-agent",
                "tags": ["durable"],
            },
        )
        doc_id = write_payload["id"]

        task_payload = await _call_tool(
            first,
            "lithos_task_create",
            {
                "title": "Restart Durable Task",
                "agent": "restart-agent",
                "description": "Ensure coordination persistence.",
            },
        )
        task_id = task_payload["task_id"]
        await _call_tool(
            first,
            "lithos_task_claim",
            {
                "task_id": task_id,
                "aspect": "verification",
                "agent": "restart-agent",
                "ttl_minutes": 30,
            },
        )
        first.stop_file_watcher()

        second = LithosServer(test_config)
        await second.initialize()

        read_payload = await _call_tool(second, "lithos_read", {"id": doc_id})
        assert read_payload["title"] == "Restart Durable Doc"

        await _wait_for_full_text_hit(second, "survive restart", doc_id)

        status_payload = await _call_tool(second, "lithos_task_status", {"task_id": task_id})
        assert len(status_payload["tasks"]) == 1
        assert status_payload["tasks"][0]["id"] == task_id
        assert any(c["aspect"] == "verification" for c in status_payload["tasks"][0]["claims"])
        second.stop_file_watcher()

    @pytest.mark.asyncio
    async def test_rebuild_skips_malformed_files(self, test_config: LithosConfig):
        """Rebuild indices gracefully skips malformed files and indexes valid ones."""
        first = LithosServer(test_config)
        await first.initialize()

        ids = []
        for i in range(2):
            payload = await _call_tool(
                first,
                "lithos_write",
                {
                    "title": f"Valid Doc {i}",
                    "content": f"Valid searchable content number {i}.",
                    "agent": "rebuild-agent",
                },
            )
            ids.append(payload["id"])
        first.stop_file_watcher()

        # Inject malformed markdown files into the knowledge directory.
        # broken-yaml.md has invalid YAML and will be skipped by _rebuild_indices.
        # binary-file.md is not valid text and will fail parsing.
        knowledge_dir = test_config.storage.knowledge_path
        (knowledge_dir / "broken-yaml.md").write_text(
            "---\n: broken yaml\n---\nSome content\n"
        )
        (knowledge_dir / "binary-file.md").write_bytes(b"\x00\x01\x02\xff\xfe")

        # Delete graph cache to force _rebuild_indices on next server init.
        graph_cache = test_config.storage.graph_path / "graph.pickle"
        if graph_cache.exists():
            graph_cache.unlink()

        second = LithosServer(test_config)
        await second.initialize()

        # Both valid docs should be readable and searchable.
        for doc_id in ids:
            read_payload = await _call_tool(second, "lithos_read", {"id": doc_id})
            assert read_payload["id"] == doc_id

        await _wait_for_full_text_hit(second, "Valid searchable content number 0", ids[0])
        await _wait_for_full_text_hit(second, "Valid searchable content number 1", ids[1])

        # Graph should contain the 2 valid docs. Malformed files should not cause
        # initialization failure (the broken-yaml error is logged and skipped).
        stats = second.graph.get_stats()
        assert stats["nodes"] >= 2

        # The 2 valid docs should be in the graph.
        for doc_id in ids:
            assert second.graph.has_node(doc_id)

        second.stop_file_watcher()


class TestFileWatcherRace:
    """Race-focused file update/delete consistency checks."""

    @pytest.mark.asyncio
    async def test_rapid_update_then_delete_keeps_indices_consistent(self, server: LithosServer):
        doc = await server.knowledge.create(
            title="Watcher Race Doc",
            content="initial",
            agent="race-agent",
            path="watched",
        )
        server.search.index_document(doc)
        server.graph.add_document(doc)

        file_path = server.config.storage.knowledge_path / doc.path
        handler = _FileChangeHandler(server, asyncio.get_running_loop())

        for i in range(10):
            await server.knowledge.update(id=doc.id, agent="race-agent", content=f"v{i}")
            handler._schedule_update(file_path, deleted=False)

        # Simulate noisy file-system ordering near deletion.
        file_path.unlink()
        handler._schedule_update(file_path, deleted=False)
        handler._schedule_update(file_path, deleted=True)
        handler._schedule_update(file_path, deleted=True)

        for _ in range(30):
            search_payload = await _call_tool(server, "lithos_search", {"query": "Watcher Race Doc"})
            in_search = any(item["id"] == doc.id for item in search_payload["results"])
            in_graph = server.graph.has_node(doc.id)
            try:
                await server.knowledge.read(id=doc.id)
                in_knowledge = True
            except FileNotFoundError:
                in_knowledge = False

            if not in_knowledge and not in_search and not in_graph:
                return
            await asyncio.sleep(0.05)

        raise AssertionError(
            "Final state inconsistent after rapid update/delete "
            f"(knowledge={in_knowledge}, search={in_search}, graph={in_graph})"
        )


class TestConcurrencyContention:
    """Contention tests for concurrent MCP operations."""

    @pytest.mark.asyncio
    async def test_parallel_updates_same_document_remain_consistent(self, server: LithosServer):
        created = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Concurrent Update Doc",
                "content": "initial content",
                "agent": "concurrency-agent",
                "tags": ["concurrency"],
            },
        )
        doc_id = created["id"]

        updates = [
            _call_tool(
                server,
                "lithos_write",
                {
                    "id": doc_id,
                    "title": "Concurrent Update Doc",
                    "content": f"content version {i}",
                    "agent": "concurrency-agent",
                    "tags": ["concurrency", f"v{i}"],
                },
            )
            for i in range(12)
        ]
        results = await asyncio.gather(*updates, return_exceptions=True)
        errors = [r for r in results if isinstance(r, Exception)]
        assert not errors, f"Unexpected tool errors under contention: {errors!r}"

        # Final document should remain readable and structurally valid.
        read_payload = await _call_tool(server, "lithos_read", {"id": doc_id})
        assert read_payload["id"] == doc_id
        assert read_payload["title"] == "Concurrent Update Doc"
        assert read_payload["content"].startswith("content version ")

        # Exactly one document should exist at this path/logical target.
        listing = await _call_tool(server, "lithos_list", {"path_prefix": ""})
        same_title = [item for item in listing["items"] if item["title"] == "Concurrent Update Doc"]
        assert len(same_title) == 1
        assert same_title[0]["id"] == doc_id

    @pytest.mark.asyncio
    async def test_parallel_claims_single_winner(self, server: LithosServer):
        task = await _call_tool(
            server,
            "lithos_task_create",
            {
                "title": "Concurrency Claim Task",
                "agent": "planner",
                "description": "Only one claim should win for same aspect.",
            },
        )
        task_id = task["task_id"]

        claim_attempts = [
            _call_tool(
                server,
                "lithos_task_claim",
                {
                    "task_id": task_id,
                    "aspect": "implementation",
                    "agent": f"worker-{i}",
                    "ttl_minutes": 15,
                },
            )
            for i in range(8)
        ]
        claim_results = await asyncio.gather(*claim_attempts)
        success_count = sum(1 for result in claim_results if result["success"])
        assert success_count == 1

        status = await _call_tool(server, "lithos_task_status", {"task_id": task_id})
        claims = status["tasks"][0]["claims"]
        assert len(claims) == 1
        assert claims[0]["aspect"] == "implementation"


class TestFrontmatterPreservation:
    """Tests for frontmatter field preservation across read-write cycles."""

    @pytest.mark.asyncio
    async def test_unknown_fields_survive_update(self, server: LithosServer):
        """Extra frontmatter fields injected on disk should survive an MCP update."""
        write_payload = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Field Preservation Test",
                "content": "Testing extra field survival.",
                "agent": "field-agent",
                "tags": ["preserve"],
            },
        )
        doc_id = write_payload["id"]
        file_path = server.config.storage.knowledge_path / write_payload["path"]

        # Manually inject unknown fields into the raw frontmatter on disk.
        post = frontmatter.load(file_path)
        post.metadata["source_url"] = "https://example.com/article"
        post.metadata["custom_score"] = 42
        file_path.write_text(frontmatter.dumps(post))

        # Verify the fields are on disk before the update.
        reloaded = frontmatter.load(file_path)
        assert reloaded.metadata["source_url"] == "https://example.com/article"
        assert reloaded.metadata["custom_score"] == 42

        # Update the document through MCP (changes content but not the extra fields).
        await _call_tool(
            server,
            "lithos_write",
            {
                "id": doc_id,
                "title": "Field Preservation Test",
                "content": "Updated content — extra fields should survive.",
                "agent": "field-agent",
            },
        )

        # Re-read the raw file — extra fields should still be present.
        after_update = frontmatter.load(file_path)
        assert after_update.metadata.get("source_url") == "https://example.com/article"
        assert after_update.metadata.get("custom_score") == 42


class TestUpdateSemantics:
    """Tests for omit-vs-replace update semantics through the MCP boundary."""

    @pytest.mark.asyncio
    async def test_omit_tags_preserves_existing(self, server: LithosServer):
        """Omitting tags from an update preserves the original tags."""
        write_payload = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Omit Tags Doc",
                "content": "Original content.",
                "agent": "semantics-agent",
                "tags": ["alpha", "beta"],
            },
        )
        doc_id = write_payload["id"]

        # Update without passing tags.
        await _call_tool(
            server,
            "lithos_write",
            {
                "id": doc_id,
                "title": "Omit Tags Doc",
                "content": "Updated content.",
                "agent": "semantics-agent",
            },
        )

        read_payload = await _call_tool(server, "lithos_read", {"id": doc_id})
        assert sorted(read_payload["metadata"]["tags"]) == ["alpha", "beta"]

    @pytest.mark.asyncio
    async def test_explicit_tags_replaces_existing(self, server: LithosServer):
        """Providing tags on update fully replaces the original set."""
        write_payload = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Replace Tags Doc",
                "content": "Original content.",
                "agent": "semantics-agent",
                "tags": ["old-tag"],
            },
        )
        doc_id = write_payload["id"]

        await _call_tool(
            server,
            "lithos_write",
            {
                "id": doc_id,
                "title": "Replace Tags Doc",
                "content": "Updated content.",
                "agent": "semantics-agent",
                "tags": ["new-tag"],
            },
        )

        read_payload = await _call_tool(server, "lithos_read", {"id": doc_id})
        assert read_payload["metadata"]["tags"] == ["new-tag"]

    @pytest.mark.asyncio
    async def test_confidence_preserved_when_omitted(self, server: LithosServer):
        """Omitting confidence from an update preserves the original value."""
        write_payload = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Confidence Preserve Doc",
                "content": "Original content.",
                "agent": "semantics-agent",
                "confidence": 0.5,
            },
        )
        doc_id = write_payload["id"]

        read_before = await _call_tool(server, "lithos_read", {"id": doc_id})
        assert read_before["metadata"]["confidence"] == 0.5

        # Update omitting confidence — should preserve the original 0.5.
        await _call_tool(
            server,
            "lithos_write",
            {
                "id": doc_id,
                "title": "Confidence Preserve Doc",
                "content": "Updated content.",
                "agent": "semantics-agent",
            },
        )

        read_after = await _call_tool(server, "lithos_read", {"id": doc_id})
        assert read_after["metadata"]["confidence"] == 0.5


class TestGraphEdgeConsistency:
    """Tests for graph edge correctness through the MCP write pipeline."""

    @pytest.mark.asyncio
    async def test_update_content_changes_graph_edges(self, server: LithosServer):
        """Updating wiki-links in content should swap graph edges accordingly."""
        alpha = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Target Alpha",
                "content": "I am target alpha.",
                "agent": "graph-agent",
            },
        )
        beta = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Target Beta",
                "content": "I am target beta.",
                "agent": "graph-agent",
            },
        )
        linker = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Linker Doc",
                "content": "This links to [[target-alpha]] for reference.",
                "agent": "graph-agent",
            },
        )
        alpha_id, beta_id, linker_id = alpha["id"], beta["id"], linker["id"]

        # Before update: linker -> alpha exists, linker -> beta does not.
        assert server.graph.has_edge(linker_id, alpha_id)
        assert not server.graph.has_edge(linker_id, beta_id)

        links_before = await _call_tool(
            server, "lithos_links", {"id": linker_id, "direction": "outgoing"}
        )
        before_ids = [link["id"] for link in links_before["outgoing"]]
        assert alpha_id in before_ids
        assert beta_id not in before_ids

        # Update linker to point to beta instead.
        await _call_tool(
            server,
            "lithos_write",
            {
                "id": linker_id,
                "title": "Linker Doc",
                "content": "This now links to [[target-beta]] instead.",
                "agent": "graph-agent",
            },
        )

        # After update: linker -> alpha gone, linker -> beta present.
        assert not server.graph.has_edge(linker_id, alpha_id)
        assert server.graph.has_edge(linker_id, beta_id)

        links_after = await _call_tool(
            server, "lithos_links", {"id": linker_id, "direction": "outgoing"}
        )
        after_ids = [link["id"] for link in links_after["outgoing"]]
        assert beta_id in after_ids
        assert alpha_id not in after_ids

        # Verify incoming links on targets are consistent.
        alpha_incoming = await _call_tool(
            server, "lithos_links", {"id": alpha_id, "direction": "incoming"}
        )
        assert not any(link["id"] == linker_id for link in alpha_incoming["incoming"])

        beta_incoming = await _call_tool(
            server, "lithos_links", {"id": beta_id, "direction": "incoming"}
        )
        assert any(link["id"] == linker_id for link in beta_incoming["incoming"])


def test_conformance_module_exists():
    """Sanity check to keep this module discoverable in test listings."""
    assert Path(__file__).name == "test_integration_conformance.py"
