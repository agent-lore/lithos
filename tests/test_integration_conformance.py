"""Integration conformance tests focused on MCP boundary contracts."""

import asyncio
import json
from datetime import datetime, timezone
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
            server,
            "lithos_search",
            {"query": query, "limit": 10, "threshold": threshold, "mode": "semantic"},
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
            server,
            "lithos_search",
            {"query": query, "limit": 10, "threshold": threshold, "mode": "semantic"},
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
        assert set(write_payload) >= {"status", "id", "path", "warnings"}
        assert write_payload["status"] == "created"
        assert isinstance(write_payload["id"], str)
        assert write_payload["path"].endswith(".md")
        assert write_payload["path"].startswith("conformance/")
        assert isinstance(write_payload["warnings"], list)

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

        delete_payload = await _call_tool(
            server, "lithos_delete", {"id": doc_id, "agent": "test-agent"}
        )
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

        await _call_tool(server, "lithos_delete", {"id": doc_id, "agent": "test-agent"})

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

        await _call_tool(server, "lithos_delete", {"id": doc_id, "agent": "test-agent"})

        # Knowledge layer: read should return structured error envelope.
        read_result = await _call_tool(server, "lithos_read", {"id": doc_id})
        assert read_result["status"] == "error"
        assert read_result["code"] == "doc_not_found"

        # Full-text search: absent.
        ft_payload = await _call_tool(
            server, "lithos_search", {"query": "neural network architecture", "limit": 10}
        )
        assert not any(item["id"] == doc_id for item in ft_payload["results"])

        # Semantic search: absent.
        sem_payload = await _call_tool(
            server,
            "lithos_search",
            {"query": "neural network deep learning", "limit": 10, "mode": "semantic"},
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
        graph_cache = test_config.storage.graph_path / "graph.json"
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
        doc = (
            await server.knowledge.create(
                title="Watcher Race Doc",
                content="initial",
                agent="race-agent",
                path="watched",
            )
        ).document
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
        success_count = sum(1 for result in claim_results if result.get("success"))
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
        post.metadata["custom_vendor"] = "acme-corp"
        post.metadata["custom_score"] = 42
        file_path.write_text(frontmatter.dumps(post))

        # Verify the fields are on disk before the update.
        reloaded = frontmatter.load(file_path)
        assert reloaded.metadata["custom_vendor"] == "acme-corp"
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
        assert after_update.metadata.get("custom_vendor") == "acme-corp"
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
            server, "lithos_related", {"id": linker_id, "include": ["links"]}
        )
        before_ids = [link["id"] for link in links_before["links"]["outgoing"]]
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
            server, "lithos_related", {"id": linker_id, "include": ["links"]}
        )
        after_ids = [link["id"] for link in links_after["links"]["outgoing"]]
        assert beta_id in after_ids
        assert alpha_id not in after_ids

        # Verify incoming links on targets are consistent.
        alpha_incoming = await _call_tool(
            server, "lithos_related", {"id": alpha_id, "include": ["links"]}
        )
        assert not any(link["id"] == linker_id for link in alpha_incoming["links"]["incoming"])

        beta_incoming = await _call_tool(
            server, "lithos_related", {"id": beta_id, "include": ["links"]}
        )
        assert any(link["id"] == linker_id for link in beta_incoming["links"]["incoming"])


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
        assert released_again["status"] == "error"

        completed = await _call_tool(
            server, "lithos_task_complete", {"task_id": task_id, "agent": "worker-a"}
        )
        assert completed["success"] is True

        # Completing an already-completed task should fail.
        completed_again = await _call_tool(
            server, "lithos_task_complete", {"task_id": task_id, "agent": "worker-a"}
        )
        assert completed_again["status"] == "error"

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

        for key in [
            "documents",
            "chroma_chunk_count",
            "agents",
            "active_tasks",
            "open_claims",
            "tags",
        ]:
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
            "lithos_search",
            {
                "query": "bioluminescent organisms",
                "tags": ["land"],
                "limit": 10,
                "mode": "semantic",
            },
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
            "lithos_search",
            {
                "query": "superconducting magnets particle accelerators",
                "threshold": 0.99,
                "limit": 10,
                "mode": "semantic",
            },
        )
        low_threshold = await _call_tool(
            server,
            "lithos_search",
            {
                "query": "superconducting magnets particle accelerators",
                "threshold": 0.0,
                "limit": 10,
                "mode": "semantic",
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


class TestRelatedLinksDepth:
    """Tests for lithos_related (links section) with depth > 1 and both directions."""

    @pytest.mark.asyncio
    async def test_links_both_directions(self, server: LithosServer):
        """lithos_related returns wiki-link neighbours in both directions at depth=1."""
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

        result = await _call_tool(
            server, "lithos_related", {"id": b["id"], "include": ["links"], "depth": 1}
        )
        outgoing_ids = [link["id"] for link in result["links"]["outgoing"]]
        incoming_ids = [link["id"] for link in result["links"]["incoming"]]

        assert c["id"] in outgoing_ids
        assert a["id"] in incoming_ids

    @pytest.mark.asyncio
    async def test_links_depth_2_traversal(self, server: LithosServer):
        """depth=2 returns transitive neighbours; depth=1 does not."""
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

        # Depth 1 from root should find middle but not leaf.
        d1 = await _call_tool(
            server, "lithos_related", {"id": a["id"], "include": ["links"], "depth": 1}
        )
        d1_ids = [link["id"] for link in d1["links"]["outgoing"]]
        assert b["id"] in d1_ids
        assert c["id"] not in d1_ids

        # Depth 2 from root should find both middle and leaf.
        d2 = await _call_tool(
            server, "lithos_related", {"id": a["id"], "include": ["links"], "depth": 2}
        )
        d2_ids = [link["id"] for link in d2["links"]["outgoing"]]
        assert b["id"] in d2_ids
        assert c["id"] in d2_ids

    @pytest.mark.asyncio
    async def test_links_depth_2_both_directions(self, server: LithosServer):
        """depth=2 covers transitive links in both directions from a mid-chain node."""
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

        # From mid at depth=2: outgoing should reach end, incoming should reach start.
        result = await _call_tool(
            server, "lithos_related", {"id": b["id"], "include": ["links"], "depth": 2}
        )
        outgoing_ids = [link["id"] for link in result["links"]["outgoing"]]
        incoming_ids = [link["id"] for link in result["links"]["incoming"]]

        assert c["id"] in outgoing_ids
        assert a["id"] in incoming_ids


class TestErrorAndBoundaryConditions:
    """Error handling and boundary condition tests through the MCP boundary."""

    @pytest.mark.asyncio
    async def test_read_nonexistent_id_raises(self, server: LithosServer):
        """lithos_read with a non-existent UUID returns a structured error envelope."""
        fake_id = "00000000-0000-0000-0000-000000000000"
        result = await _call_tool(server, "lithos_read", {"id": fake_id})
        assert result["status"] == "error"
        assert result["code"] == "doc_not_found"

    @pytest.mark.asyncio
    async def test_read_nonexistent_path_raises(self, server: LithosServer):
        """lithos_read with a non-existent path returns a structured error envelope."""
        result = await _call_tool(server, "lithos_read", {"path": "no-such/file.md"})
        assert result["status"] == "error"
        assert result["code"] == "doc_not_found"

    @pytest.mark.asyncio
    async def test_delete_nonexistent_id_returns_false(self, server: LithosServer):
        """lithos_delete with a non-existent UUID returns an error envelope."""
        fake_id = "00000000-0000-0000-0000-000000000000"
        result = await _call_tool(server, "lithos_delete", {"id": fake_id, "agent": "test-agent"})
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_claim_nonexistent_task_returns_false(self, server: LithosServer):
        """lithos_task_claim on a non-existent task returns an error envelope."""
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
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_complete_nonexistent_task_returns_false(self, server: LithosServer):
        """lithos_task_complete on a non-existent task returns an error envelope."""
        result = await _call_tool(
            server,
            "lithos_task_complete",
            {"task_id": "nonexistent-task-id", "agent": "err-agent"},
        )
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_release_nonexistent_claim_returns_false(self, server: LithosServer):
        """lithos_task_release on a non-existent claim returns an error envelope."""
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
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_renew_nonexistent_claim_returns_false(self, server: LithosServer):
        """lithos_task_renew on a non-existent claim returns an error envelope."""
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
        assert result["status"] == "error"

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

        await _call_tool(server, "lithos_delete", {"id": doc_id, "agent": "test-agent"})

        tags_after = await _call_tool(server, "lithos_tags", {})
        assert tags_after["tags"].get(unique_tag, 0) == 0


class TestSourceUrlMCPResponses:
    """Integration tests for source_url in MCP tool responses and write status envelopes."""

    @pytest.mark.asyncio
    async def test_write_created_status_with_source_url(self, server: LithosServer):
        """lithos_write returns status=created with source_url stored."""
        payload = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Source URL Created Doc",
                "content": "Document with provenance URL.",
                "agent": "source-url-agent",
                "source_url": "https://example.com/article?utm_source=test",
            },
        )
        assert payload["status"] == "created"
        assert "id" in payload
        assert "path" in payload
        assert isinstance(payload["warnings"], list)

        # Verify the normalized URL is stored (tracking param stripped)
        read = await _call_tool(server, "lithos_read", {"id": payload["id"]})
        assert read["metadata"]["source_url"] == "https://example.com/article"

    @pytest.mark.asyncio
    async def test_write_updated_status(self, server: LithosServer):
        """lithos_write returns status=updated when updating an existing doc."""
        created = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Update Status Doc",
                "content": "Original content.",
                "agent": "source-url-agent",
                "source_url": "https://example.com/update-test",
            },
        )
        assert created["status"] == "created"
        doc_id = created["id"]

        updated = await _call_tool(
            server,
            "lithos_write",
            {
                "id": doc_id,
                "title": "Update Status Doc",
                "content": "Revised content.",
                "agent": "source-url-agent",
            },
        )
        assert updated["status"] == "updated"
        assert updated["id"] == doc_id
        assert isinstance(updated["warnings"], list)

    @pytest.mark.asyncio
    async def test_write_duplicate_status(self, server: LithosServer):
        """lithos_write returns status=duplicate when URL collides with existing doc."""
        first = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Original URL Doc",
                "content": "First document with this URL.",
                "agent": "source-url-agent",
                "source_url": "https://example.com/unique-dedup-test",
            },
        )
        assert first["status"] == "created"

        dup = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Duplicate URL Doc",
                "content": "Second document trying same URL.",
                "agent": "source-url-agent",
                "source_url": "https://example.com/unique-dedup-test",
            },
        )
        assert dup["status"] == "duplicate"
        assert "duplicate_of" in dup
        assert dup["duplicate_of"]["id"] == first["id"]
        assert dup["duplicate_of"]["title"] == "Original URL Doc"
        assert dup["duplicate_of"]["source_url"] == "https://example.com/unique-dedup-test"
        assert "message" in dup
        assert isinstance(dup["warnings"], list)

    @pytest.mark.asyncio
    async def test_write_invalid_source_url(self, server: LithosServer):
        """lithos_write returns error status for invalid source_url."""
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Invalid URL Doc",
                "content": "Should fail.",
                "agent": "source-url-agent",
                "source_url": "ftp://not-http.example.com/file",
            },
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"
        assert "message" in result
        assert isinstance(result["warnings"], list)

    @pytest.mark.asyncio
    async def test_write_clear_source_url(self, server: LithosServer):
        """lithos_write with source_url="" clears existing source_url and frees dedup slot."""
        created = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Clearable URL Doc",
                "content": "Has a source_url initially.",
                "agent": "source-url-agent",
                "source_url": "https://example.com/clearable",
            },
        )
        assert created["status"] == "created"
        doc_id = created["id"]

        # Clear source_url by passing empty string
        updated = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Clearable URL Doc",
                "content": "Source URL cleared.",
                "agent": "source-url-agent",
                "id": doc_id,
                "source_url": "",
            },
        )
        assert updated["status"] == "updated"

        # Read back — source_url should be None
        read_result = await _call_tool(server, "lithos_read", {"id": doc_id})
        assert read_result["metadata"]["source_url"] is None

        # Dedup slot freed — another doc can now claim the same URL
        second = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Reuse Cleared URL",
                "content": "Takes over the freed URL.",
                "agent": "source-url-agent",
                "source_url": "https://example.com/clearable",
            },
        )
        assert second["status"] == "created"

    @pytest.mark.asyncio
    async def test_read_includes_source_url(self, server: LithosServer):
        """lithos_read metadata includes source_url."""
        created = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Read Source URL Doc",
                "content": "Content with URL provenance.",
                "agent": "source-url-agent",
                "source_url": "https://example.com/read-test",
            },
        )
        read = await _call_tool(server, "lithos_read", {"id": created["id"]})
        assert read["metadata"]["source_url"] == "https://example.com/read-test"

    @pytest.mark.asyncio
    async def test_read_null_source_url(self, server: LithosServer):
        """lithos_read returns null source_url for docs without one."""
        created = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "No URL Doc",
                "content": "No source_url set.",
                "agent": "source-url-agent",
            },
        )
        read = await _call_tool(server, "lithos_read", {"id": created["id"]})
        assert read["metadata"]["source_url"] is None

    @pytest.mark.asyncio
    async def test_search_includes_source_url_and_staleness(self, server: LithosServer):
        """lithos_search results include source_url, updated_at, and is_stale."""
        created = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Search Provenance Doc",
                "content": "Unique magnetohydrodynamic plasma containment research.",
                "agent": "source-url-agent",
                "source_url": "https://example.com/search-provenance",
            },
        )
        doc_id = created["id"]
        await _wait_for_full_text_hit(server, "magnetohydrodynamic plasma", doc_id)

        search = await _call_tool(
            server, "lithos_search", {"query": "magnetohydrodynamic plasma", "limit": 10}
        )
        matched = [r for r in search["results"] if r["id"] == doc_id]
        assert len(matched) == 1
        assert matched[0]["source_url"] == "https://example.com/search-provenance"
        assert "updated_at" in matched[0]
        assert "is_stale" in matched[0]
        assert isinstance(matched[0]["is_stale"], bool)

    @pytest.mark.asyncio
    async def test_semantic_includes_source_url_and_staleness(self, server: LithosServer):
        """lithos_semantic results include source_url, updated_at, and is_stale."""
        created = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Semantic Provenance Doc",
                "content": "Cryogenic turbopump engineering for orbital launch vehicles.",
                "agent": "source-url-agent",
                "source_url": "https://example.com/semantic-provenance",
            },
        )
        doc_id = created["id"]
        await _wait_for_semantic_hit(server, "cryogenic turbopump launch vehicles", doc_id)

        semantic = await _call_tool(
            server,
            "lithos_search",
            {"query": "cryogenic turbopump launch vehicles", "limit": 10, "mode": "semantic"},
        )
        matched = [r for r in semantic["results"] if r["id"] == doc_id]
        assert len(matched) == 1
        assert matched[0]["source_url"] == "https://example.com/semantic-provenance"
        assert "updated_at" in matched[0]
        assert "is_stale" in matched[0]
        assert isinstance(matched[0]["is_stale"], bool)

    @pytest.mark.asyncio
    async def test_list_includes_source_url(self, server: LithosServer):
        """lithos_list items include source_url."""
        created = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "List Provenance Doc",
                "content": "Listed document with URL.",
                "agent": "source-url-agent",
                "tags": ["list-provenance-test"],
                "source_url": "https://example.com/list-provenance",
            },
        )
        doc_id = created["id"]

        listing = await _call_tool(
            server, "lithos_list", {"tags": ["list-provenance-test"], "limit": 50}
        )
        matched = [item for item in listing["items"] if item["id"] == doc_id]
        assert len(matched) == 1
        assert matched[0]["source_url"] == "https://example.com/list-provenance"

    @pytest.mark.asyncio
    async def test_stats_includes_duplicate_urls(self, server: LithosServer):
        """lithos_stats includes duplicate_urls count."""
        stats = await _call_tool(server, "lithos_stats", {})
        assert "duplicate_urls" in stats
        assert isinstance(stats["duplicate_urls"], int)

    @pytest.mark.asyncio
    async def test_stats_health_indicators_present(self, server: LithosServer):
        """lithos_stats includes all health indicator fields."""
        stats = await _call_tool(server, "lithos_stats", {})

        # Index drift
        assert "index_drift_detected" in stats
        assert isinstance(stats["index_drift_detected"], bool)

        # Tantivy doc count may be None when index is not yet written
        assert "tantivy_doc_count" in stats
        assert stats["tantivy_doc_count"] is None or isinstance(stats["tantivy_doc_count"], int)

        # Chroma chunk count
        assert "chroma_chunk_count" in stats
        assert isinstance(stats["chroma_chunk_count"], int)

        # Unresolved links
        assert "unresolved_links" in stats
        assert isinstance(stats["unresolved_links"], int)

        # Expired docs
        assert "expired_docs" in stats
        assert isinstance(stats["expired_docs"], int)

        # Expired claims
        assert "expired_claims" in stats
        assert isinstance(stats["expired_claims"], int)

        # Index timestamps (None if not yet created)
        assert "tantivy_last_updated" in stats
        assert stats["tantivy_last_updated"] is None or isinstance(
            stats["tantivy_last_updated"], str
        )
        assert "chroma_last_updated" in stats
        assert stats["chroma_last_updated"] is None or isinstance(stats["chroma_last_updated"], str)

        # Graph stats
        assert "graph_node_count" in stats
        assert isinstance(stats["graph_node_count"], int)
        assert "graph_edge_count" in stats
        assert isinstance(stats["graph_edge_count"], int)

    @pytest.mark.asyncio
    async def test_stats_expired_docs_counts_stale(self, server: LithosServer):
        """lithos_stats.expired_docs reflects expired documents."""
        from datetime import timedelta

        stats_before = await _call_tool(server, "lithos_stats", {})
        expired_before = stats_before["expired_docs"]

        # Create a document that is already expired
        await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Expired Health Doc",
                "content": "This document is already expired.",
                "agent": "health-agent",
                "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            },
        )

        stats_after = await _call_tool(server, "lithos_stats", {})
        assert stats_after["expired_docs"] == expired_before + 1

    @pytest.mark.asyncio
    async def test_stats_no_drift_when_indices_consistent(self, server: LithosServer):
        """index_drift_detected is False when Tantivy and knowledge are in sync.

        We verify the field exists and has a boolean type; an empty test
        database may have tantivy_doc_count=None (index not written yet) so
        drift will be False by definition in that case.
        """
        stats = await _call_tool(server, "lithos_stats", {})
        assert isinstance(stats["index_drift_detected"], bool)
        # If tantivy index is not yet populated, drift cannot be detected
        if stats["tantivy_doc_count"] is None:
            assert stats["index_drift_detected"] is False

    @pytest.mark.asyncio
    async def test_stats_drift_detected_when_tantivy_count_differs(
        self, server: LithosServer, monkeypatch
    ):
        """index_drift_detected is True when Tantivy count diverges from KnowledgeManager.

        We create one document so KnowledgeManager.document_count == 1, then
        monkeypatch tantivy.count_docs() to return a stale value (0).  The
        drift flag must be set to True in the lithos_stats response.
        """
        # Seed one document so the knowledge manager has a non-zero document count.
        await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Drift Detection Seed Doc",
                "content": "Intentionally unseeded in Tantivy to trigger drift.",
                "agent": "drift-test-agent",
            },
        )
        assert server.knowledge.document_count >= 1

        # Simulate a stale / out-of-sync Tantivy index by making count_docs()
        # return a value that does NOT match the in-memory document count.
        stale_count = server.knowledge.document_count - 1
        monkeypatch.setattr(server.search.tantivy, "count_docs", lambda: stale_count)

        stats = await _call_tool(server, "lithos_stats", {})

        assert stats["tantivy_doc_count"] == stale_count
        assert stats["index_drift_detected"] is True


class TestDerivedFromIdsMCPBoundary:
    """Integration tests for US-008: derived_from_ids through lithos_write MCP tool."""

    @pytest.mark.asyncio
    async def test_create_with_derived_from_ids(self, server: LithosServer):
        """lithos_write create with derived_from_ids stores and reads back."""
        src = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Provenance Source",
                "content": "Source document.",
                "agent": "prov-agent",
            },
        )
        src_id = src["id"]

        derived = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Provenance Derived",
                "content": "Derived from source.",
                "agent": "prov-agent",
                "derived_from_ids": [src_id],
            },
        )
        assert derived["status"] == "created"

        read = await _call_tool(server, "lithos_read", {"id": derived["id"]})
        assert read["metadata"]["derived_from_ids"] == [src_id]

    @pytest.mark.asyncio
    async def test_update_omit_preserves_derived_from_ids(self, server: LithosServer):
        """lithos_write update without derived_from_ids preserves existing."""
        src = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Preserve Source",
                "content": "Source.",
                "agent": "prov-agent",
            },
        )

        doc = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Preserve Derived",
                "content": "Derived.",
                "agent": "prov-agent",
                "derived_from_ids": [src["id"]],
            },
        )

        # Update without passing derived_from_ids (None at MCP boundary = preserve)
        await _call_tool(
            server,
            "lithos_write",
            {
                "id": doc["id"],
                "title": "Preserve Derived",
                "content": "Updated content.",
                "agent": "prov-agent",
            },
        )

        read = await _call_tool(server, "lithos_read", {"id": doc["id"]})
        assert read["metadata"]["derived_from_ids"] == [src["id"]]

    @pytest.mark.asyncio
    async def test_update_clear_derived_from_ids(self, server: LithosServer):
        """lithos_write update with derived_from_ids=[] clears provenance."""
        src = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Clear Source",
                "content": "Source.",
                "agent": "prov-agent",
            },
        )

        doc = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Clear Derived",
                "content": "Derived.",
                "agent": "prov-agent",
                "derived_from_ids": [src["id"]],
            },
        )

        # Clear by passing empty list
        await _call_tool(
            server,
            "lithos_write",
            {
                "id": doc["id"],
                "title": "Clear Derived",
                "content": "Cleared.",
                "agent": "prov-agent",
                "derived_from_ids": [],
            },
        )

        read = await _call_tool(server, "lithos_read", {"id": doc["id"]})
        # to_dict() omits empty lists, so key may be absent
        assert read["metadata"].get("derived_from_ids", []) == []

    @pytest.mark.asyncio
    async def test_update_replace_derived_from_ids(self, server: LithosServer):
        """lithos_write update with non-empty derived_from_ids replaces."""
        s1 = await _call_tool(
            server,
            "lithos_write",
            {"title": "Replace S1", "content": "S1.", "agent": "prov-agent"},
        )
        s2 = await _call_tool(
            server,
            "lithos_write",
            {"title": "Replace S2", "content": "S2.", "agent": "prov-agent"},
        )

        doc = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Replace Derived",
                "content": "Derived.",
                "agent": "prov-agent",
                "derived_from_ids": [s1["id"]],
            },
        )

        # Replace s1 with s2
        await _call_tool(
            server,
            "lithos_write",
            {
                "id": doc["id"],
                "title": "Replace Derived",
                "content": "Replaced.",
                "agent": "prov-agent",
                "derived_from_ids": [s2["id"]],
            },
        )

        read = await _call_tool(server, "lithos_read", {"id": doc["id"]})
        assert read["metadata"]["derived_from_ids"] == [s2["id"]]

    @pytest.mark.asyncio
    async def test_create_with_unresolved_returns_warnings(self, server: LithosServer):
        """lithos_write create with missing source IDs returns warnings."""
        fake_id = "00000000-0000-0000-0000-111111111111"
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Unresolved Provenance",
                "content": "References missing source.",
                "agent": "prov-agent",
                "derived_from_ids": [fake_id],
            },
        )
        assert result["status"] == "created"
        assert any(fake_id in w for w in result["warnings"])

    @pytest.mark.asyncio
    async def test_create_with_invalid_uuid_returns_error(self, server: LithosServer):
        """lithos_write create with invalid UUID in derived_from_ids returns error."""
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Invalid UUID Provenance",
                "content": "Bad reference.",
                "agent": "prov-agent",
                "derived_from_ids": ["not-a-uuid"],
            },
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"


class TestSyncFromDisk:
    """Integration tests for US-009: sync_from_disk provenance index maintenance."""

    @pytest.mark.asyncio
    async def test_external_create_with_derived_from_ids(self, server: LithosServer):
        """Externally created file with derived_from_ids updates provenance indexes."""
        import uuid

        # Create a source doc via MCP
        src = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Sync Source",
                "content": "Source document.",
                "agent": "sync-agent",
            },
        )
        src_id = src["id"]

        # Externally create a file on disk with derived_from_ids
        doc_id = str(uuid.uuid4())
        post = frontmatter.Post(
            "# External Derived\n\nDerived from source via external file.",
            id=doc_id,
            title="External Derived",
            author="external",
            created_at="2026-03-08T00:00:00+00:00",
            updated_at="2026-03-08T00:00:00+00:00",
            tags=[],
            aliases=[],
            confidence=1.0,
            contributors=[],
            source=None,
            supersedes=None,
            derived_from_ids=[src_id],
        )
        knowledge_path = server.config.storage.knowledge_path
        file_path = knowledge_path / "external-derived.md"
        file_path.write_text(frontmatter.dumps(post))

        # Call handle_file_change to simulate watcher
        await server.handle_file_change(file_path, deleted=False)

        # Verify provenance indexes are updated
        assert doc_id in server.knowledge._doc_to_sources
        assert server.knowledge._doc_to_sources[doc_id] == [src_id]
        assert doc_id in server.knowledge._source_to_derived.get(src_id, set())
        assert server.knowledge._id_to_title[doc_id] == "External Derived"

    @pytest.mark.asyncio
    async def test_external_modify_derived_from_ids(self, server: LithosServer):
        """Externally modifying derived_from_ids in a file updates indexes."""
        # Create source docs and a derived doc via MCP
        s1 = await _call_tool(
            server,
            "lithos_write",
            {"title": "Modify Source 1", "content": "S1.", "agent": "sync-agent"},
        )
        s2 = await _call_tool(
            server,
            "lithos_write",
            {"title": "Modify Source 2", "content": "S2.", "agent": "sync-agent"},
        )
        derived = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Modifiable Derived",
                "content": "Derived from S1.",
                "agent": "sync-agent",
                "derived_from_ids": [s1["id"]],
            },
        )
        derived_id = derived["id"]
        derived_path_str = derived["path"]

        # Verify initial provenance
        assert server.knowledge._doc_to_sources[derived_id] == [s1["id"]]
        assert derived_id in server.knowledge._source_to_derived.get(s1["id"], set())

        # Externally modify the file's derived_from_ids on disk
        knowledge_path = server.config.storage.knowledge_path
        file_path = knowledge_path / derived_path_str
        post = frontmatter.load(str(file_path))
        post.metadata["derived_from_ids"] = [s2["id"]]
        file_path.write_text(frontmatter.dumps(post))

        # Simulate watcher event
        await server.handle_file_change(file_path, deleted=False)

        # Verify indexes updated: s1 removed, s2 added
        assert server.knowledge._doc_to_sources[derived_id] == [s2["id"]]
        assert derived_id not in server.knowledge._source_to_derived.get(s1["id"], set())
        assert derived_id in server.knowledge._source_to_derived.get(s2["id"], set())

    @pytest.mark.asyncio
    async def test_external_title_change_updates_id_to_title(self, server: LithosServer):
        """Externally changing a file's title updates _id_to_title."""
        doc = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Original Title",
                "content": "Content.",
                "agent": "sync-agent",
            },
        )
        doc_id = doc["id"]
        doc_path_str = doc["path"]

        assert server.knowledge._id_to_title[doc_id] == "Original Title"

        # Externally modify the title on disk
        knowledge_path = server.config.storage.knowledge_path
        file_path = knowledge_path / doc_path_str
        post = frontmatter.load(str(file_path))
        post.metadata["title"] = "Changed Title"
        # Also update H1 to match
        post.content = "# Changed Title\n\nContent."
        file_path.write_text(frontmatter.dumps(post))

        await server.handle_file_change(file_path, deleted=False)

        assert server.knowledge._id_to_title[doc_id] == "Changed Title"

    @pytest.mark.asyncio
    async def test_external_delete_source_marks_unresolved(self, server: LithosServer):
        """Externally deleting a source doc marks provenance as unresolved."""
        # Create source + derived via MCP
        src = await _call_tool(
            server,
            "lithos_write",
            {"title": "Deletable Source", "content": "Source.", "agent": "sync-agent"},
        )
        src_id = src["id"]
        src_path_str = src["path"]

        derived = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Orphanable Derived",
                "content": "Derived from deletable source.",
                "agent": "sync-agent",
                "derived_from_ids": [src_id],
            },
        )
        derived_id = derived["id"]

        # Verify initial state
        assert derived_id in server.knowledge._source_to_derived.get(src_id, set())

        # Externally delete the source file
        knowledge_path = server.config.storage.knowledge_path
        src_file = knowledge_path / src_path_str
        src_file.unlink()

        await server.handle_file_change(src_file, deleted=True)

        # Source should be removed from indexes
        assert src_id not in server.knowledge._id_to_path
        assert src_id not in server.knowledge._id_to_title

        # Derived doc's provenance should now be unresolved
        assert derived_id in server.knowledge._unresolved_provenance.get(src_id, set())
        assert src_id not in server.knowledge._source_to_derived

    @pytest.mark.asyncio
    async def test_sync_from_disk_auto_resolves_unresolved(self, server: LithosServer):
        """Externally creating a file that matches an unresolved ref auto-resolves it."""
        import uuid

        # Create a derived doc that references a not-yet-existing source
        future_source_id = str(uuid.uuid4())
        derived = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Waiting Derived",
                "content": "Waiting for source to appear.",
                "agent": "sync-agent",
                "derived_from_ids": [future_source_id],
            },
        )
        derived_id = derived["id"]

        # Verify unresolved
        assert derived_id in server.knowledge._unresolved_provenance.get(future_source_id, set())

        # Now externally create the source file with the matching ID
        post = frontmatter.Post(
            "# Future Source\n\nNow exists on disk.",
            id=future_source_id,
            title="Future Source",
            author="external",
            created_at="2026-03-08T00:00:00+00:00",
            updated_at="2026-03-08T00:00:00+00:00",
            tags=[],
            aliases=[],
            confidence=1.0,
            contributors=[],
            source=None,
            supersedes=None,
        )
        knowledge_path = server.config.storage.knowledge_path
        file_path = knowledge_path / "future-source.md"
        file_path.write_text(frontmatter.dumps(post))

        await server.handle_file_change(file_path, deleted=False)

        # Unresolved should now be resolved
        assert future_source_id not in server.knowledge._unresolved_provenance
        assert derived_id in server.knowledge._source_to_derived.get(future_source_id, set())

    @pytest.mark.asyncio
    async def test_sync_from_disk_error_does_not_crash_watcher(self, server: LithosServer):
        """File watcher provenance errors are caught and logged, never crash."""
        knowledge_path = server.config.storage.knowledge_path

        # Create a malformed file
        bad_file = knowledge_path / "malformed-sync.md"
        bad_file.write_bytes(b"\x00\x01\x02\xff\xfe")

        # Should not raise
        await server.handle_file_change(bad_file, deleted=False)

        # Verify server is still functional
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Post Error Doc",
                "content": "Server still works.",
                "agent": "sync-agent",
            },
        )
        assert result["status"] == "created"


class TestRebuildIndicesProvenance:
    """Tests for US-010: _rebuild_indices provenance support."""

    async def test_rebuild_indices_restores_provenance_indexes(self, server: LithosServer):
        """After _rebuild_indices(), provenance indexes match on-disk state."""
        # Create source doc
        source_result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Rebuild Source",
                "content": "Source content.",
                "agent": "rebuild-agent",
            },
        )
        assert source_result["status"] == "created"
        source_id = source_result["id"]

        # Create derived doc referencing the source
        derived_result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Rebuild Derived",
                "content": "Derived content.",
                "agent": "rebuild-agent",
                "derived_from_ids": [source_id],
            },
        )
        assert derived_result["status"] == "created"
        derived_id = derived_result["id"]

        # Verify provenance indexes before rebuild
        mgr = server.knowledge
        assert mgr._doc_to_sources.get(derived_id) == [source_id]
        assert derived_id in mgr._source_to_derived.get(source_id, set())
        assert mgr._id_to_title.get(source_id) == "Rebuild Source"
        assert mgr._id_to_title.get(derived_id) == "Rebuild Derived"

        # Rebuild indices
        await server._rebuild_indices()

        # Verify provenance indexes are restored after rebuild
        assert mgr._doc_to_sources.get(derived_id) == [source_id]
        assert derived_id in mgr._source_to_derived.get(source_id, set())
        assert mgr._id_to_title.get(source_id) == "Rebuild Source"
        assert mgr._id_to_title.get(derived_id) == "Rebuild Derived"
        assert not mgr._unresolved_provenance  # no unresolved refs

    async def test_rebuild_indices_detects_unresolved_provenance(self, server: LithosServer):
        """After _rebuild_indices(), unresolved references are correctly detected."""
        missing_id = "00000000-0000-0000-0000-000000000099"

        # Create a doc referencing a non-existent source
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Unresolved After Rebuild",
                "content": "References a missing doc.",
                "agent": "rebuild-agent",
                "derived_from_ids": [missing_id],
            },
        )
        assert result["status"] == "created"
        doc_id = result["id"]

        # Rebuild indices
        await server._rebuild_indices()

        # Verify unresolved provenance is detected
        mgr = server.knowledge
        assert mgr._doc_to_sources.get(doc_id) == [missing_id]
        assert doc_id in mgr._unresolved_provenance.get(missing_id, set())
        assert missing_id not in mgr._source_to_derived

    async def test_rebuild_indices_clears_stale_provenance(self, server: LithosServer):
        """_rebuild_indices() clears stale provenance from a previous rebuild."""
        # Create a doc with provenance
        source_result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Stale Source",
                "content": "Source.",
                "agent": "rebuild-agent",
            },
        )
        source_id = source_result["id"]

        derived_result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Stale Derived",
                "content": "Derived.",
                "agent": "rebuild-agent",
                "derived_from_ids": [source_id],
            },
        )
        derived_id = derived_result["id"]

        # Delete the derived doc from disk (simulating external deletion)
        mgr = server.knowledge
        derived_path = mgr._id_to_path[derived_id]
        full_path = mgr.knowledge_path / derived_path
        full_path.unlink()

        # Rebuild — should not have stale provenance entries for deleted doc
        await server._rebuild_indices()

        # The derived doc should no longer be in any indexes
        assert derived_id not in mgr._doc_to_sources
        assert derived_id not in mgr._source_to_derived.get(source_id, set())
        assert derived_id not in mgr._id_to_title


class TestRelatedProvenance:
    """Tests for the provenance section of lithos_related (US-011 coverage)."""

    async def _create_doc(
        self,
        server: LithosServer,
        title: str,
        derived_from_ids: list[str] | None = None,
    ) -> str:
        """Helper to create a doc and return its ID."""
        args: dict[str, Any] = {
            "title": title,
            "content": f"Content of {title}.",
            "agent": "prov-agent",
        }
        if derived_from_ids is not None:
            args["derived_from_ids"] = derived_from_ids
        result = await _call_tool(server, "lithos_write", args)
        assert result["status"] == "created"
        return result["id"]

    async def _prov(self, server: LithosServer, doc_id: str, *, depth: int = 1) -> dict[str, Any]:
        result = await _call_tool(
            server,
            "lithos_related",
            {"id": doc_id, "include": ["provenance"], "depth": depth},
        )
        assert "provenance" in result, result
        return result["provenance"]

    async def test_single_depth_sources(self, server: LithosServer):
        """Immediate source document shows up under provenance.sources."""
        source_id = await self._create_doc(server, "Prov Source A")
        derived_id = await self._create_doc(server, "Prov Derived A", derived_from_ids=[source_id])

        prov = await self._prov(server, derived_id)
        assert len(prov["sources"]) == 1
        assert prov["sources"][0]["id"] == source_id
        assert prov["sources"][0]["title"] == "Prov Source A"
        # No incoming derives from this leaf doc.
        assert prov["derived"] == []

    async def test_single_depth_derived(self, server: LithosServer):
        """Immediate derived document shows up under provenance.derived."""
        source_id = await self._create_doc(server, "Prov Source B")
        derived_id = await self._create_doc(server, "Prov Derived B", derived_from_ids=[source_id])

        prov = await self._prov(server, source_id)
        assert prov["sources"] == []
        assert len(prov["derived"]) == 1
        assert prov["derived"][0]["id"] == derived_id

    async def test_multi_depth_bfs_chain(self, server: LithosServer):
        """depth=2 BFS traverses A->B->C chain in both directions."""
        a_id = await self._create_doc(server, "Chain A")
        b_id = await self._create_doc(server, "Chain B", derived_from_ids=[a_id])
        c_id = await self._create_doc(server, "Chain C", derived_from_ids=[b_id])

        # From A at depth=2 the derived list should reach B and C.
        prov_a = await self._prov(server, a_id, depth=2)
        derived_ids = [n["id"] for n in prov_a["derived"]]
        assert b_id in derived_ids
        assert c_id in derived_ids

        # From C at depth=2 the sources list should reach B and A.
        prov_c = await self._prov(server, c_id, depth=2)
        source_ids = [n["id"] for n in prov_c["sources"]]
        assert b_id in source_ids
        assert a_id in source_ids

    async def test_cycle_handling(self, server: LithosServer):
        """Cycles (A->B, B->A) don't cause infinite traversal."""
        a_id = await self._create_doc(server, "Cycle A")
        b_id = await self._create_doc(server, "Cycle B", derived_from_ids=[a_id])

        # Update A to also derive from B (creates a cycle).
        await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Cycle A",
                "content": "Updated.",
                "agent": "prov-agent",
                "id": a_id,
                "derived_from_ids": [b_id],
            },
        )

        # Should return without hanging at depth=3.
        prov = await self._prov(server, a_id, depth=3)
        source_ids = [n["id"] for n in prov["sources"]]
        derived_ids = [n["id"] for n in prov["derived"]]
        assert b_id in source_ids
        assert b_id in derived_ids

    async def test_unresolved_sources_included(self, server: LithosServer):
        """Unresolved source UUIDs surface under provenance.unresolved_sources."""
        missing_id = "00000000-0000-0000-0000-000000aaaaaa"
        doc_id = await self._create_doc(server, "Unresolved Prov", derived_from_ids=[missing_id])

        prov = await self._prov(server, doc_id)
        assert missing_id in prov["unresolved_sources"]

    async def test_both_direction(self, server: LithosServer):
        """provenance includes sources and derived without an explicit flag."""
        a_id = await self._create_doc(server, "Both Source")
        b_id = await self._create_doc(server, "Both Middle", derived_from_ids=[a_id])
        c_id = await self._create_doc(server, "Both Derived", derived_from_ids=[b_id])

        prov = await self._prov(server, b_id)
        source_ids = [n["id"] for n in prov["sources"]]
        derived_ids = [n["id"] for n in prov["derived"]]
        assert a_id in source_ids
        assert c_id in derived_ids

    async def test_sources_sorted_by_id(self, server: LithosServer):
        """Sources list is sorted by ID for deterministic output."""
        s1 = await self._create_doc(server, "Sort Source 1")
        s2 = await self._create_doc(server, "Sort Source 2")
        doc_id = await self._create_doc(server, "Sort Derived", derived_from_ids=[s1, s2])

        prov = await self._prov(server, doc_id)
        ids = [n["id"] for n in prov["sources"]]
        assert ids == sorted(ids)


class TestDerivedFromIdsInResponses:
    """Tests for US-012: derived_from_ids in read/search/list responses."""

    async def test_lithos_read_includes_derived_from_ids(self, server: LithosServer):
        """lithos_read response metadata includes derived_from_ids."""
        source_result = await _call_tool(
            server,
            "lithos_write",
            {"title": "Read Source", "content": "Source.", "agent": "resp-agent"},
        )
        source_id = source_result["id"]

        derived_result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Read Derived",
                "content": "Derived.",
                "agent": "resp-agent",
                "derived_from_ids": [source_id],
            },
        )
        derived_id = derived_result["id"]

        # Read derived doc
        read_result = await _call_tool(server, "lithos_read", {"id": derived_id})
        assert read_result["metadata"]["derived_from_ids"] == [source_id]

        # Read source doc (no provenance)
        read_result = await _call_tool(server, "lithos_read", {"id": source_id})
        assert read_result["metadata"]["derived_from_ids"] == []

    async def test_lithos_search_includes_derived_from_ids(self, server: LithosServer):
        """lithos_search result items include derived_from_ids."""
        source_result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Search Prov Source",
                "content": "Unique provenance search content zyx987.",
                "agent": "resp-agent",
            },
        )
        source_id = source_result["id"]

        derived_result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Search Prov Derived",
                "content": "Unique provenance search derived zyx987.",
                "agent": "resp-agent",
                "derived_from_ids": [source_id],
            },
        )
        derived_id = derived_result["id"]

        await _wait_for_full_text_hit(server, "zyx987", derived_id)

        search_result = await _call_tool(server, "lithos_search", {"query": "zyx987", "limit": 10})
        found = [r for r in search_result["results"] if r["id"] == derived_id]
        assert len(found) == 1
        assert found[0]["derived_from_ids"] == [source_id]

        # Source doc should have empty derived_from_ids
        source_found = [r for r in search_result["results"] if r["id"] == source_id]
        if source_found:
            assert source_found[0]["derived_from_ids"] == []

    async def test_lithos_list_includes_derived_from_ids(self, server: LithosServer):
        """lithos_list result items include derived_from_ids."""
        source_result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "List Prov Source",
                "content": "Source.",
                "agent": "resp-agent",
                "tags": ["list-prov-test"],
            },
        )
        source_id = source_result["id"]

        derived_result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "List Prov Derived",
                "content": "Derived.",
                "agent": "resp-agent",
                "tags": ["list-prov-test"],
                "derived_from_ids": [source_id],
            },
        )
        derived_id = derived_result["id"]

        list_result = await _call_tool(server, "lithos_list", {"tags": ["list-prov-test"]})
        items = {i["id"]: i for i in list_result["items"]}
        assert items[derived_id]["derived_from_ids"] == [source_id]
        assert items[source_id]["derived_from_ids"] == []


class TestLithosRelated:
    """Tests for #82 lithos_related — composite links + provenance + edges."""

    async def _create_doc(
        self,
        server: LithosServer,
        title: str,
        *,
        content: str | None = None,
        derived_from_ids: list[str] | None = None,
    ) -> str:
        args: dict[str, Any] = {
            "title": title,
            "content": content if content is not None else f"Content of {title}.",
            "agent": "related-agent",
        }
        if derived_from_ids is not None:
            args["derived_from_ids"] = derived_from_ids
        result = await _call_tool(server, "lithos_write", args)
        assert result["status"] == "created"
        return result["id"]

    async def test_unknown_id_returns_doc_not_found(self, server: LithosServer):
        result = await _call_tool(
            server, "lithos_related", {"id": "00000000-0000-4000-8000-000000000000"}
        )
        assert result["status"] == "error"
        assert result["code"] == "doc_not_found"

    async def test_default_include_covers_all_three_sections(self, server: LithosServer):
        """Omitting include returns links + provenance + edges (all three)."""
        doc = await self._create_doc(server, "Related Default")
        result = await _call_tool(server, "lithos_related", {"id": doc})
        assert result["id"] == doc
        assert set(result["included"]) == {"links", "provenance", "edges"}
        assert "links" in result
        assert "provenance" in result
        assert "edges" in result
        assert result["related_ids"] == []  # isolated doc

    async def test_include_subset_omits_unselected_sections(self, server: LithosServer):
        doc = await self._create_doc(server, "Related Subset")
        result = await _call_tool(server, "lithos_related", {"id": doc, "include": ["links"]})
        assert result["included"] == ["links"]
        assert "links" in result
        assert "provenance" not in result
        assert "edges" not in result

    async def test_unknown_include_values_are_silently_ignored(self, server: LithosServer):
        doc = await self._create_doc(server, "Related Forward Compat")
        result = await _call_tool(
            server, "lithos_related", {"id": doc, "include": ["links", "future_backend"]}
        )
        assert result["included"] == ["links"]

    async def test_wiki_links_populate_links_section(self, server: LithosServer):
        """An outgoing [[wiki-link]] shows up under links.outgoing."""
        target = await self._create_doc(server, "Related Target Note")
        linker = await self._create_doc(
            server,
            "Related Linker Note",
            content="See [[related-target-note]] for reference.",
        )
        result = await _call_tool(server, "lithos_related", {"id": linker})
        outgoing_ids = [n["id"] for n in result["links"]["outgoing"]]
        assert target in outgoing_ids
        assert target in result["related_ids"]

    async def test_derived_from_populates_provenance_section(self, server: LithosServer):
        """derived_from_ids shows up under provenance.sources (and reverse)."""
        source = await self._create_doc(server, "Source Note")
        derived = await self._create_doc(server, "Derived Note", derived_from_ids=[source])

        result_derived = await _call_tool(server, "lithos_related", {"id": derived})
        source_ids = [n["id"] for n in result_derived["provenance"]["sources"]]
        assert source in source_ids
        assert source in result_derived["related_ids"]

        result_source = await _call_tool(server, "lithos_related", {"id": source})
        derived_ids = [n["id"] for n in result_source["provenance"]["derived"]]
        assert derived in derived_ids
        assert derived in result_source["related_ids"]

    async def test_depth_is_clamped_to_valid_range(self, server: LithosServer):
        """depth=0 or depth=99 must not blow up — clamped to 1..3."""
        doc = await self._create_doc(server, "Depth Clamp")
        zero = await _call_tool(server, "lithos_related", {"id": doc, "depth": 0})
        huge = await _call_tool(server, "lithos_related", {"id": doc, "depth": 99})
        assert "links" in zero and "links" in huge

    async def test_related_ids_exclude_self(self, server: LithosServer):
        """Self-id must never appear in related_ids even if a self-link exists."""
        doc_id = await self._create_doc(server, "Self Exclude")
        result = await _call_tool(server, "lithos_related", {"id": doc_id})
        assert doc_id not in result["related_ids"]

    async def test_edges_populate_both_directions(self, server: LithosServer):
        """An LCMA edge upserted via lithos_edge_upsert shows up on both endpoints."""
        a = await self._create_doc(server, "Edge Endpoint A")
        b = await self._create_doc(server, "Edge Endpoint B")

        up = await _call_tool(
            server,
            "lithos_edge_upsert",
            {
                "from_id": a,
                "to_id": b,
                "type": "related_to",
                "weight": 0.7,
                "namespace": "default",
            },
        )
        assert up["status"] == "ok"

        # Outgoing side: A's edges include one where to_id == B.
        result_a = await _call_tool(server, "lithos_related", {"id": a})
        outgoing_tos = [e["to_id"] for e in result_a["edges"]["outgoing"]]
        assert b in outgoing_tos
        assert b in result_a["related_ids"]

        # Incoming side: B's edges include one where from_id == A.
        result_b = await _call_tool(server, "lithos_related", {"id": b})
        incoming_froms = [e["from_id"] for e in result_b["edges"]["incoming"]]
        assert a in incoming_froms
        assert a in result_b["related_ids"]

    async def test_namespace_filter_scopes_edges_only(self, server: LithosServer):
        """namespace='other' filters the edges section; links/provenance untouched."""
        a = await self._create_doc(server, "NS Scoped A")
        b = await self._create_doc(server, "NS Scoped B")
        await _call_tool(
            server,
            "lithos_edge_upsert",
            {
                "from_id": a,
                "to_id": b,
                "type": "related_to",
                "weight": 0.5,
                "namespace": "default",
            },
        )

        result = await _call_tool(server, "lithos_related", {"id": a, "namespace": "nonexistent"})
        assert result["edges"]["outgoing"] == []
        assert result["edges"]["incoming"] == []


def test_conformance_module_exists():
    """Sanity check to keep this module discoverable in test listings."""
    assert Path(__file__).name == "test_integration_conformance.py"
