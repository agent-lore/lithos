"""Integration conformance tests focused on MCP boundary contracts."""

import asyncio
import json
from pathlib import Path
from typing import Any

import frontmatter
import pytest
from fastmcp.exceptions import ToolError

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

    content = getattr(result, "content", []) if hasattr(result, "content") else result

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
    raise AssertionError(f"Document {doc_id} not found in semantic results for query={query!r}")


async def _wait_for_full_text_miss(server: LithosServer, query: str, doc_id: str) -> None:
    """Wait briefly for a document to disappear from full-text search."""
    for _ in range(20):
        payload = await _call_tool(server, "lithos_search", {"query": query, "limit": 10})
        if not any(item["id"] == doc_id for item in payload["results"]):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"Document {doc_id} still present in search results for query={query!r}")


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
    raise AssertionError(f"Document {doc_id} still present in semantic results for query={query!r}")


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
        with pytest.raises(ToolError):
            await _call_tool(server, "lithos_read", {"id": doc_id})

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
        (knowledge_dir / "broken-yaml.md").write_text("---\n: broken yaml\n---\nSome content\n")
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
            search_payload = await _call_tool(
                server, "lithos_search", {"query": "Watcher Race Doc"}
            )
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


class TestAgentAndCoordinationMCPTools:
    """Integration coverage for MCP tools not previously exercised."""

    @pytest.mark.asyncio
    async def test_integration_mcp_agents_roundtrip(self, server: LithosServer):
        created = await _call_tool(
            server,
            "lithos_agent_register",
            {
                "id": "agent-roundtrip",
                "name": "Roundtrip Agent",
                "type": "integration-test",
                "metadata": {"team": "qa"},
            },
        )
        assert created["success"] is True
        assert created["created"] is True

        updated = await _call_tool(
            server,
            "lithos_agent_register",
            {
                "id": "agent-roundtrip",
                "name": "Roundtrip Agent v2",
                "type": "integration-test",
            },
        )
        assert updated["success"] is True
        assert updated["created"] is False

        info_response = await _call_tool(server, "lithos_agent_info", {"id": "agent-roundtrip"})
        info = info_response.get("result", info_response)
        assert info["id"] == "agent-roundtrip"
        assert info["name"] == "Roundtrip Agent v2"
        assert info["type"] == "integration-test"
        assert info["first_seen_at"] is not None
        assert info["last_seen_at"] is not None

        listing = await _call_tool(server, "lithos_agent_list", {"type": "integration-test"})
        assert any(agent["id"] == "agent-roundtrip" for agent in listing["agents"])

    @pytest.mark.asyncio
    async def test_integration_mcp_task_lifecycle_full(self, server: LithosServer):
        created = await _call_tool(
            server,
            "lithos_task_create",
            {
                "title": "Lifecycle Full Task",
                "agent": "lifecycle-agent",
                "description": "Exercise claim/renew/release/complete end-to-end.",
            },
        )
        task_id = created["task_id"]

        claim = await _call_tool(
            server,
            "lithos_task_claim",
            {
                "task_id": task_id,
                "aspect": "implementation",
                "agent": "worker-a",
                "ttl_minutes": 10,
            },
        )
        assert claim["success"] is True
        first_expiry = claim["expires_at"]
        assert first_expiry is not None

        renew = await _call_tool(
            server,
            "lithos_task_renew",
            {
                "task_id": task_id,
                "aspect": "implementation",
                "agent": "worker-a",
                "ttl_minutes": 20,
            },
        )
        assert renew["success"] is True
        assert renew["new_expires_at"] is not None
        assert renew["new_expires_at"] != first_expiry

        released = await _call_tool(
            server,
            "lithos_task_release",
            {"task_id": task_id, "aspect": "implementation", "agent": "worker-a"},
        )
        assert released["success"] is True

        # Releasing again should fail cleanly.
        released_again = await _call_tool(
            server,
            "lithos_task_release",
            {"task_id": task_id, "aspect": "implementation", "agent": "worker-a"},
        )
        assert released_again["success"] is False

        completed = await _call_tool(
            server, "lithos_task_complete", {"task_id": task_id, "agent": "worker-a"}
        )
        assert completed["success"] is True

        # Completing an already-completed task should fail.
        completed_again = await _call_tool(
            server, "lithos_task_complete", {"task_id": task_id, "agent": "worker-a"}
        )
        assert completed_again["success"] is False

        status = await _call_tool(server, "lithos_task_status", {"task_id": task_id})
        assert len(status["tasks"]) == 1
        assert status["tasks"][0]["status"] == "completed"
        assert status["tasks"][0]["claims"] == []

    @pytest.mark.asyncio
    async def test_integration_mcp_findings_with_since_filter(self, server: LithosServer):
        task = await _call_tool(
            server,
            "lithos_task_create",
            {"title": "Findings Since Task", "agent": "finder-agent"},
        )
        task_id = task["task_id"]

        knowledge = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Finding Linked Knowledge",
                "content": "Knowledge linked from finding.",
                "agent": "finder-agent",
            },
        )
        knowledge_id = knowledge["id"]

        first = await _call_tool(
            server,
            "lithos_finding_post",
            {
                "task_id": task_id,
                "agent": "finder-agent",
                "summary": "Initial finding",
                "knowledge_id": knowledge_id,
            },
        )
        assert first["finding_id"]

        # Capture an exact boundary after the first finding.
        all_findings = await _call_tool(server, "lithos_finding_list", {"task_id": task_id})
        assert len(all_findings["findings"]) == 1
        since_marker = all_findings["findings"][0]["created_at"]
        assert all_findings["findings"][0]["knowledge_id"] == knowledge_id

        await _call_tool(
            server,
            "lithos_finding_post",
            {
                "task_id": task_id,
                "agent": "finder-agent",
                "summary": "Follow-up finding",
            },
        )

        filtered = await _call_tool(
            server, "lithos_finding_list", {"task_id": task_id, "since": since_marker}
        )
        assert len(filtered["findings"]) == 1
        assert filtered["findings"][0]["summary"] == "Follow-up finding"
        assert filtered["findings"][0]["knowledge_id"] is None

    @pytest.mark.asyncio
    async def test_integration_mcp_tags_and_stats_contract(self, server: LithosServer):
        tags_before = await _call_tool(server, "lithos_tags", {})
        stats_before = await _call_tool(server, "lithos_stats", {})

        for key in ["documents", "chunks", "agents", "active_tasks", "open_claims", "tags"]:
            assert key in stats_before
            assert isinstance(stats_before[key], int)

        await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Stats Contract Doc",
                "content": "This contributes to tag and document stats.",
                "agent": "stats-agent",
                "tags": ["stats", "contract"],
            },
        )
        task = await _call_tool(
            server,
            "lithos_task_create",
            {"title": "Stats Contract Task", "agent": "stats-agent"},
        )
        await _call_tool(
            server,
            "lithos_task_claim",
            {
                "task_id": task["task_id"],
                "aspect": "stats-check",
                "agent": "stats-agent",
                "ttl_minutes": 15,
            },
        )

        tags_after = await _call_tool(server, "lithos_tags", {})
        stats_after = await _call_tool(server, "lithos_stats", {})

        assert "tags" in tags_after
        assert tags_after["tags"].get("stats", 0) >= tags_before["tags"].get("stats", 0) + 1
        assert stats_after["documents"] >= stats_before["documents"] + 1
        assert stats_after["active_tasks"] >= stats_before["active_tasks"] + 1
        assert stats_after["open_claims"] >= stats_before["open_claims"] + 1

    @pytest.mark.asyncio
    async def test_integration_mcp_invalid_datetime_inputs_fail_cleanly(self, server: LithosServer):
        task = await _call_tool(
            server,
            "lithos_task_create",
            {"title": "Bad Date Task", "agent": "date-agent"},
        )
        await _call_tool(
            server,
            "lithos_finding_post",
            {
                "task_id": task["task_id"],
                "agent": "date-agent",
                "summary": "Date parsing test finding",
            },
        )

        with pytest.raises(ToolError, match="Invalid isoformat string"):
            await _call_tool(server, "lithos_list", {"since": "not-a-date"})

        with pytest.raises(ToolError, match="Invalid isoformat string"):
            await _call_tool(server, "lithos_agent_list", {"active_since": "still-not-a-date"})

        with pytest.raises(ToolError, match="Invalid isoformat string"):
            await _call_tool(
                server,
                "lithos_finding_list",
                {"task_id": task["task_id"], "since": "definitely-not-a-date"},
            )


class TestReadByPathAndTruncation:
    """Tests for lithos_read by path and max_length truncation."""

    @pytest.mark.asyncio
    async def test_read_by_path(self, server: LithosServer):
        """lithos_read resolves a document by relative path."""
        write_payload = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Read By Path Doc",
                "content": "Content for path-based lookup.",
                "agent": "path-agent",
                "path": "guides",
            },
        )
        doc_id = write_payload["id"]
        doc_path = write_payload["path"]

        read_payload = await _call_tool(server, "lithos_read", {"path": doc_path})
        assert read_payload["id"] == doc_id
        assert read_payload["title"] == "Read By Path Doc"
        assert read_payload["content"] == "Content for path-based lookup."

    @pytest.mark.asyncio
    async def test_read_by_path_without_md_suffix(self, server: LithosServer):
        """lithos_read auto-appends .md when path lacks it."""
        write_payload = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "No Suffix Doc",
                "content": "Auto-suffix test.",
                "agent": "path-agent",
            },
        )
        doc_id = write_payload["id"]
        doc_path = write_payload["path"]
        path_without_md = doc_path.removesuffix(".md")

        read_payload = await _call_tool(server, "lithos_read", {"path": path_without_md})
        assert read_payload["id"] == doc_id

    @pytest.mark.asyncio
    async def test_read_with_max_length_truncates(self, server: LithosServer):
        """lithos_read with max_length truncates content and sets truncated flag."""
        long_content = "First paragraph of important content.\n\n" + ("x" * 500)
        write_payload = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Truncation Doc",
                "content": long_content,
                "agent": "trunc-agent",
            },
        )
        doc_id = write_payload["id"]

        read_full = await _call_tool(server, "lithos_read", {"id": doc_id})
        assert read_full["truncated"] is False
        assert len(read_full["content"]) == len(long_content)

        read_truncated = await _call_tool(server, "lithos_read", {"id": doc_id, "max_length": 60})
        assert read_truncated["truncated"] is True
        assert len(read_truncated["content"]) <= 60 + 3  # allow for "..." suffix

    @pytest.mark.asyncio
    async def test_read_max_length_no_truncation_when_short(self, server: LithosServer):
        """lithos_read with max_length larger than content does not truncate."""
        write_payload = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Short Doc",
                "content": "Brief.",
                "agent": "trunc-agent",
            },
        )
        doc_id = write_payload["id"]

        read_payload = await _call_tool(server, "lithos_read", {"id": doc_id, "max_length": 1000})
        assert read_payload["truncated"] is False
        assert read_payload["content"] == "Brief."


class TestSearchAndListFilters:
    """Tests for filter parameters on search, semantic, and list tools."""

    @pytest.mark.asyncio
    async def test_search_filters_by_tags(self, server: LithosServer):
        """lithos_search tag filter narrows results to matching documents."""
        await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Tagged Alpha",
                "content": "Searchable content about filtering mechanisms.",
                "agent": "filter-agent",
                "tags": ["alpha-group"],
            },
        )
        beta = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Tagged Beta",
                "content": "Searchable content about filtering mechanisms.",
                "agent": "filter-agent",
                "tags": ["beta-group"],
            },
        )
        beta_id = beta["id"]

        await _wait_for_full_text_hit(server, "filtering mechanisms", beta_id)

        filtered = await _call_tool(
            server,
            "lithos_search",
            {"query": "filtering mechanisms", "tags": ["beta-group"]},
        )
        result_ids = [r["id"] for r in filtered["results"]]
        assert beta_id in result_ids
        assert all(r["id"] == beta_id for r in filtered["results"])

    @pytest.mark.asyncio
    async def test_search_filters_by_author(self, server: LithosServer):
        """lithos_search author filter returns only docs by that author."""
        await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Author A Doc",
                "content": "Authored content about magnetospheric resonance.",
                "agent": "author-a",
            },
        )
        b_doc = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Author B Doc",
                "content": "Authored content about magnetospheric resonance.",
                "agent": "author-b",
            },
        )
        b_id = b_doc["id"]

        await _wait_for_full_text_hit(server, "magnetospheric resonance", b_id)

        filtered = await _call_tool(
            server,
            "lithos_search",
            {"query": "magnetospheric resonance", "author": "author-b"},
        )
        result_ids = [r["id"] for r in filtered["results"]]
        assert b_id in result_ids
        # author-a doc should not appear
        for r in filtered["results"]:
            assert r["id"] == b_id

    @pytest.mark.asyncio
    async def test_search_filters_by_path_prefix(self, server: LithosServer):
        """lithos_search path_prefix filter restricts to a subdirectory."""
        await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Procedures Item",
                "content": "Photovoltaic cell efficiency measurements.",
                "agent": "prefix-agent",
                "path": "procedures",
            },
        )
        other = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Guides Item",
                "content": "Photovoltaic cell efficiency measurements.",
                "agent": "prefix-agent",
                "path": "guides",
            },
        )
        other_id = other["id"]

        await _wait_for_full_text_hit(server, "photovoltaic cell efficiency", other_id)

        filtered = await _call_tool(
            server,
            "lithos_search",
            {"query": "photovoltaic cell efficiency", "path_prefix": "procedures"},
        )
        for r in filtered["results"]:
            assert r["path"].startswith("procedures/")

    @pytest.mark.asyncio
    async def test_semantic_search_filters_by_tags(self, server: LithosServer):
        """lithos_semantic tag filter narrows semantic results."""
        await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Semantic Tag A",
                "content": "Bioluminescent organisms in deep ocean trenches.",
                "agent": "sem-filter-agent",
                "tags": ["ocean"],
            },
        )
        land_doc = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Semantic Tag B",
                "content": "Bioluminescent fungi in terrestrial cave systems.",
                "agent": "sem-filter-agent",
                "tags": ["land"],
            },
        )
        land_id = land_doc["id"]

        await _wait_for_semantic_hit(server, "bioluminescent organisms", land_id)

        filtered = await _call_tool(
            server,
            "lithos_semantic",
            {"query": "bioluminescent organisms", "tags": ["land"], "limit": 10},
        )
        result_ids = [r["id"] for r in filtered["results"]]
        assert land_id in result_ids

    @pytest.mark.asyncio
    async def test_semantic_search_with_threshold(self, server: LithosServer):
        """lithos_semantic threshold filters out low-similarity results."""
        write_payload = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "High Similarity Doc",
                "content": "Superconducting magnets used in particle accelerators.",
                "agent": "threshold-agent",
            },
        )
        doc_id = write_payload["id"]

        await _wait_for_semantic_hit(
            server, "superconducting magnets particle accelerators", doc_id
        )

        # Very high threshold should return fewer or no results
        high_threshold = await _call_tool(
            server,
            "lithos_semantic",
            {
                "query": "superconducting magnets particle accelerators",
                "threshold": 0.99,
                "limit": 10,
            },
        )
        low_threshold = await _call_tool(
            server,
            "lithos_semantic",
            {
                "query": "superconducting magnets particle accelerators",
                "threshold": 0.0,
                "limit": 10,
            },
        )
        assert len(low_threshold["results"]) >= len(high_threshold["results"])

    @pytest.mark.asyncio
    async def test_list_filters_by_tags(self, server: LithosServer):
        """lithos_list tag filter returns only matching documents."""
        unique_tag = "list-filter-unique-tag"
        tagged = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "List Tagged Doc",
                "content": "Content for list tag filter.",
                "agent": "list-agent",
                "tags": [unique_tag],
            },
        )
        await _call_tool(
            server,
            "lithos_write",
            {
                "title": "List Untagged Doc",
                "content": "Content without the unique tag.",
                "agent": "list-agent",
                "tags": ["other"],
            },
        )

        filtered = await _call_tool(server, "lithos_list", {"tags": [unique_tag], "limit": 50})
        assert filtered["total"] >= 1
        assert all(unique_tag in item["tags"] for item in filtered["items"])
        assert any(item["id"] == tagged["id"] for item in filtered["items"])

    @pytest.mark.asyncio
    async def test_list_filters_by_author(self, server: LithosServer):
        """lithos_list author filter returns only docs by that author."""
        await _call_tool(
            server,
            "lithos_write",
            {
                "title": "List Author X Doc",
                "content": "By author X.",
                "agent": "author-x",
            },
        )
        y_doc = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "List Author Y Doc",
                "content": "By author Y.",
                "agent": "author-y",
            },
        )

        filtered = await _call_tool(server, "lithos_list", {"author": "author-y", "limit": 50})
        assert filtered["total"] >= 1
        assert any(item["id"] == y_doc["id"] for item in filtered["items"])

    @pytest.mark.asyncio
    async def test_list_filters_by_since(self, server: LithosServer):
        """lithos_list since filter returns only recently updated docs."""
        old_doc = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Old List Doc",
                "content": "Created before the cutoff.",
                "agent": "since-agent",
            },
        )
        old_id = old_doc["id"]

        await asyncio.sleep(0.05)

        # Use current time as the cutoff (after old doc was created).
        from datetime import datetime, timezone

        cutoff = datetime.now(timezone.utc).isoformat()

        await asyncio.sleep(0.02)

        new_doc = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "New List Doc",
                "content": "Created after the cutoff.",
                "agent": "since-agent",
            },
        )
        new_id = new_doc["id"]

        filtered = await _call_tool(server, "lithos_list", {"since": cutoff, "limit": 50})
        item_ids = [item["id"] for item in filtered["items"]]
        assert new_id in item_ids
        assert old_id not in item_ids

    @pytest.mark.asyncio
    async def test_list_pagination_with_offset(self, server: LithosServer):
        """lithos_list offset+limit implements correct pagination."""
        ids = []
        for i in range(5):
            doc = await _call_tool(
                server,
                "lithos_write",
                {
                    "title": f"Paginated Doc {i}",
                    "content": f"Pagination test content {i}.",
                    "agent": "page-agent",
                    "tags": ["pagination-test"],
                },
            )
            ids.append(doc["id"])

        page1 = await _call_tool(
            server,
            "lithos_list",
            {"tags": ["pagination-test"], "limit": 2, "offset": 0},
        )
        page2 = await _call_tool(
            server,
            "lithos_list",
            {"tags": ["pagination-test"], "limit": 2, "offset": 2},
        )

        assert page1["total"] >= 5
        assert len(page1["items"]) == 2
        assert len(page2["items"]) == 2
        # Pages should not overlap
        page1_ids = {item["id"] for item in page1["items"]}
        page2_ids = {item["id"] for item in page2["items"]}
        assert page1_ids.isdisjoint(page2_ids)

    @pytest.mark.asyncio
    async def test_agent_list_active_since_filter(self, server: LithosServer):
        """lithos_agent_list active_since filter returns recently active agents."""
        from datetime import datetime, timezone

        await _call_tool(
            server,
            "lithos_agent_register",
            {"id": "old-agent", "name": "Old Agent", "type": "test"},
        )

        await asyncio.sleep(0.05)
        cutoff = datetime.now(timezone.utc).isoformat()
        await asyncio.sleep(0.02)

        await _call_tool(
            server,
            "lithos_agent_register",
            {"id": "new-agent", "name": "New Agent", "type": "test"},
        )

        filtered = await _call_tool(server, "lithos_agent_list", {"active_since": cutoff})
        agent_ids = [a["id"] for a in filtered["agents"]]
        assert "new-agent" in agent_ids
        assert "old-agent" not in agent_ids


class TestLinksDepthAndDirection:
    """Tests for lithos_links with depth > 1 and direction=both."""

    @pytest.mark.asyncio
    async def test_links_direction_both(self, server: LithosServer):
        """lithos_links direction=both returns outgoing and incoming."""
        a = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Node A",
                "content": "Links to [[node-b]].",
                "agent": "link-agent",
            },
        )
        b = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Node B",
                "content": "Links to [[node-c]].",
                "agent": "link-agent",
            },
        )
        c = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Node C",
                "content": "Terminal node.",
                "agent": "link-agent",
            },
        )

        links = await _call_tool(
            server, "lithos_links", {"id": b["id"], "direction": "both", "depth": 1}
        )
        outgoing_ids = [link["id"] for link in links["outgoing"]]
        incoming_ids = [link["id"] for link in links["incoming"]]

        assert c["id"] in outgoing_ids
        assert a["id"] in incoming_ids

    @pytest.mark.asyncio
    async def test_links_depth_2_traversal(self, server: LithosServer):
        """lithos_links depth=2 returns transitive neighbors."""
        a = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Depth Root",
                "content": "Links to [[depth-middle]].",
                "agent": "depth-agent",
            },
        )
        b = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Depth Middle",
                "content": "Links to [[depth-leaf]].",
                "agent": "depth-agent",
            },
        )
        c = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Depth Leaf",
                "content": "No further links.",
                "agent": "depth-agent",
            },
        )

        # Depth 1 from root should find middle but not leaf
        links_d1 = await _call_tool(
            server, "lithos_links", {"id": a["id"], "direction": "outgoing", "depth": 1}
        )
        d1_ids = [link["id"] for link in links_d1["outgoing"]]
        assert b["id"] in d1_ids
        assert c["id"] not in d1_ids

        # Depth 2 from root should find both middle and leaf
        links_d2 = await _call_tool(
            server, "lithos_links", {"id": a["id"], "direction": "outgoing", "depth": 2}
        )
        d2_ids = [link["id"] for link in links_d2["outgoing"]]
        assert b["id"] in d2_ids
        assert c["id"] in d2_ids

    @pytest.mark.asyncio
    async def test_links_depth_2_both_directions(self, server: LithosServer):
        """lithos_links depth=2 direction=both returns transitive links in both directions."""
        a = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Chain Start",
                "content": "Links to [[chain-mid]].",
                "agent": "chain-agent",
            },
        )
        b = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Chain Mid",
                "content": "Links to [[chain-end]].",
                "agent": "chain-agent",
            },
        )
        c = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Chain End",
                "content": "Terminal.",
                "agent": "chain-agent",
            },
        )

        # From mid at depth=2: outgoing should reach end, incoming should reach start
        links = await _call_tool(
            server, "lithos_links", {"id": b["id"], "direction": "both", "depth": 2}
        )
        outgoing_ids = [link["id"] for link in links["outgoing"]]
        incoming_ids = [link["id"] for link in links["incoming"]]

        assert c["id"] in outgoing_ids
        assert a["id"] in incoming_ids


class TestErrorAndBoundaryConditions:
    """Error handling and boundary condition tests through the MCP boundary."""

    @pytest.mark.asyncio
    async def test_read_nonexistent_id_raises(self, server: LithosServer):
        """lithos_read with a non-existent UUID raises an error."""
        fake_id = "00000000-0000-0000-0000-000000000000"
        with pytest.raises(ToolError):
            await _call_tool(server, "lithos_read", {"id": fake_id})

    @pytest.mark.asyncio
    async def test_read_nonexistent_path_raises(self, server: LithosServer):
        """lithos_read with a non-existent path raises an error."""
        with pytest.raises(ToolError):
            await _call_tool(server, "lithos_read", {"path": "no-such/file.md"})

    @pytest.mark.asyncio
    async def test_delete_nonexistent_id_returns_false(self, server: LithosServer):
        """lithos_delete with a non-existent UUID returns success=False."""
        fake_id = "00000000-0000-0000-0000-000000000000"
        result = await _call_tool(server, "lithos_delete", {"id": fake_id})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_claim_nonexistent_task_returns_false(self, server: LithosServer):
        """lithos_task_claim on a non-existent task returns success=False."""
        result = await _call_tool(
            server,
            "lithos_task_claim",
            {
                "task_id": "nonexistent-task-id",
                "aspect": "work",
                "agent": "err-agent",
                "ttl_minutes": 10,
            },
        )
        assert result["success"] is False
        assert result["expires_at"] is None

    @pytest.mark.asyncio
    async def test_complete_nonexistent_task_returns_false(self, server: LithosServer):
        """lithos_task_complete on a non-existent task returns success=False."""
        result = await _call_tool(
            server,
            "lithos_task_complete",
            {"task_id": "nonexistent-task-id", "agent": "err-agent"},
        )
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_release_nonexistent_claim_returns_false(self, server: LithosServer):
        """lithos_task_release on a non-existent claim returns success=False."""
        task = await _call_tool(
            server,
            "lithos_task_create",
            {"title": "Release Error Task", "agent": "err-agent"},
        )
        result = await _call_tool(
            server,
            "lithos_task_release",
            {
                "task_id": task["task_id"],
                "aspect": "unclaimed-aspect",
                "agent": "err-agent",
            },
        )
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_renew_nonexistent_claim_returns_false(self, server: LithosServer):
        """lithos_task_renew on a non-existent claim returns success=False."""
        task = await _call_tool(
            server,
            "lithos_task_create",
            {"title": "Renew Error Task", "agent": "err-agent"},
        )
        result = await _call_tool(
            server,
            "lithos_task_renew",
            {
                "task_id": task["task_id"],
                "aspect": "unclaimed-aspect",
                "agent": "err-agent",
                "ttl_minutes": 10,
            },
        )
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_write_with_source_task_persists(self, server: LithosServer):
        """lithos_write source_task parameter is stored in metadata."""
        task = await _call_tool(
            server,
            "lithos_task_create",
            {"title": "Source Task", "agent": "source-agent"},
        )
        task_id = task["task_id"]

        doc = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Sourced Doc",
                "content": "This doc came from a task.",
                "agent": "source-agent",
                "source_task": task_id,
            },
        )

        read_payload = await _call_tool(server, "lithos_read", {"id": doc["id"]})
        assert read_payload["metadata"].get("source") == task_id


class TestCrossConcernMutationAssertions:
    """Tests for cross-cutting mutation side effects (timestamps, contributors, tag counts)."""

    @pytest.mark.asyncio
    async def test_update_advances_updated_at(self, server: LithosServer):
        """Updating a document advances its updated_at timestamp."""
        doc = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Timestamp Doc",
                "content": "Original content.",
                "agent": "ts-agent",
            },
        )
        doc_id = doc["id"]

        read_before = await _call_tool(server, "lithos_read", {"id": doc_id})
        ts_before = read_before["metadata"]["updated_at"]

        await asyncio.sleep(0.02)

        await _call_tool(
            server,
            "lithos_write",
            {
                "id": doc_id,
                "title": "Timestamp Doc",
                "content": "Updated content.",
                "agent": "ts-agent",
            },
        )

        read_after = await _call_tool(server, "lithos_read", {"id": doc_id})
        ts_after = read_after["metadata"]["updated_at"]

        assert ts_after > ts_before

    @pytest.mark.asyncio
    async def test_update_by_different_agent_adds_contributor(self, server: LithosServer):
        """Updating a document by a different agent adds them to contributors."""
        doc = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Contributors Doc",
                "content": "Created by agent-one.",
                "agent": "agent-one",
            },
        )
        doc_id = doc["id"]

        read_before = await _call_tool(server, "lithos_read", {"id": doc_id})
        assert read_before["metadata"]["author"] == "agent-one"
        assert "agent-two" not in read_before["metadata"].get("contributors", [])

        await _call_tool(
            server,
            "lithos_write",
            {
                "id": doc_id,
                "title": "Contributors Doc",
                "content": "Updated by agent-two.",
                "agent": "agent-two",
            },
        )

        read_after = await _call_tool(server, "lithos_read", {"id": doc_id})
        assert "agent-two" in read_after["metadata"]["contributors"]

    @pytest.mark.asyncio
    async def test_update_reflects_in_list_updated_field(self, server: LithosServer):
        """lithos_list returns the updated timestamp that matches the latest write."""
        doc = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "List Updated Doc",
                "content": "Original.",
                "agent": "list-ts-agent",
                "tags": ["list-updated-check"],
            },
        )
        doc_id = doc["id"]

        await asyncio.sleep(0.02)

        await _call_tool(
            server,
            "lithos_write",
            {
                "id": doc_id,
                "title": "List Updated Doc",
                "content": "Revised.",
                "agent": "list-ts-agent",
            },
        )

        read_payload = await _call_tool(server, "lithos_read", {"id": doc_id})
        expected_ts = read_payload["metadata"]["updated_at"]

        list_payload = await _call_tool(
            server, "lithos_list", {"tags": ["list-updated-check"], "limit": 50}
        )
        matched = [item for item in list_payload["items"] if item["id"] == doc_id]
        assert len(matched) == 1
        assert matched[0]["updated"] == expected_ts

    @pytest.mark.asyncio
    async def test_delete_last_doc_with_tag_removes_tag_from_counts(self, server: LithosServer):
        """Deleting the last document with a given tag removes it from lithos_tags."""
        unique_tag = "ephemeral-tag-for-deletion-test"
        doc = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Ephemeral Tag Doc",
                "content": "Only doc with this tag.",
                "agent": "tag-agent",
                "tags": [unique_tag],
            },
        )
        doc_id = doc["id"]

        tags_before = await _call_tool(server, "lithos_tags", {})
        assert tags_before["tags"].get(unique_tag, 0) >= 1

        await _call_tool(server, "lithos_delete", {"id": doc_id})

        tags_after = await _call_tool(server, "lithos_tags", {})
        assert tags_after["tags"].get(unique_tag, 0) == 0


def test_conformance_module_exists():
    """Sanity check to keep this module discoverable in test listings."""
    assert Path(__file__).name == "test_integration_conformance.py"
