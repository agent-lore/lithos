"""Tests for US-010: lithos_edge_upsert and lithos_edge_list MCP tools.

Integration tests that exercise edge tools via the MCP interface.
"""

import json
from typing import Any

import pytest

from lithos.server import LithosServer

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


class TestEdgeUpsertCreate:
    @pytest.mark.asyncio
    async def test_create_edge(self, server: LithosServer) -> None:
        """Create a new edge and verify edge_id returned."""
        result = await _call_tool(
            server,
            "lithos_edge_upsert",
            {
                "from_id": "node-a",
                "to_id": "node-b",
                "type": "related_to",
                "weight": 0.8,
                "namespace": "default",
            },
        )
        assert result["status"] == "ok"
        assert result["edge_id"].startswith("edge_")

    @pytest.mark.asyncio
    async def test_create_edge_with_evidence_dict(self, server: LithosServer) -> None:
        """Create edge with dict evidence."""
        result = await _call_tool(
            server,
            "lithos_edge_upsert",
            {
                "from_id": "node-c",
                "to_id": "node-d",
                "type": "derived_from",
                "weight": 1.0,
                "namespace": "research",
                "evidence": {"source": "analysis", "confidence": 0.9},
            },
        )
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_create_edge_with_evidence_list(self, server: LithosServer) -> None:
        """Create edge with list evidence."""
        result = await _call_tool(
            server,
            "lithos_edge_upsert",
            {
                "from_id": "node-e",
                "to_id": "node-f",
                "type": "supports",
                "weight": 0.5,
                "namespace": "default",
                "evidence": ["fact-1", "fact-2"],
            },
        )
        assert result["status"] == "ok"


class TestEdgeUpsertUpdate:
    @pytest.mark.asyncio
    async def test_update_by_composite_key(self, server: LithosServer) -> None:
        """Upsert with same (from_id, to_id, type, namespace) updates existing edge."""
        result1 = await _call_tool(
            server,
            "lithos_edge_upsert",
            {
                "from_id": "node-x",
                "to_id": "node-y",
                "type": "related_to",
                "weight": 0.5,
                "namespace": "default",
            },
        )
        edge_id1 = result1["edge_id"]

        result2 = await _call_tool(
            server,
            "lithos_edge_upsert",
            {
                "from_id": "node-x",
                "to_id": "node-y",
                "type": "related_to",
                "weight": 0.9,
                "namespace": "default",
            },
        )
        edge_id2 = result2["edge_id"]

        # Same edge_id means it was updated, not created
        assert edge_id1 == edge_id2

        # Verify updated weight
        list_result = await _call_tool(
            server,
            "lithos_edge_list",
            {"from_id": "node-x", "namespace": "default"},
        )
        edges = list_result["results"]
        assert len(edges) == 1
        assert edges[0]["weight"] == 0.9

    @pytest.mark.asyncio
    async def test_idempotent_upsert(self, server: LithosServer) -> None:
        """Upserting identical data is idempotent."""
        args = {
            "from_id": "node-idem",
            "to_id": "node-idem2",
            "type": "supports",
            "weight": 1.0,
            "namespace": "default",
        }
        r1 = await _call_tool(server, "lithos_edge_upsert", args)
        r2 = await _call_tool(server, "lithos_edge_upsert", args)
        assert r1["edge_id"] == r2["edge_id"]


class TestEdgeUpsertValidation:
    @pytest.mark.asyncio
    async def test_missing_namespace_returns_error(self, server: LithosServer) -> None:
        """Missing namespace returns error envelope."""
        result = await _call_tool(
            server,
            "lithos_edge_upsert",
            {
                "from_id": "node-a",
                "to_id": "node-b",
                "type": "related_to",
                "weight": 0.5,
                "namespace": "",
            },
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_scalar_evidence_returns_error(self, server: LithosServer) -> None:
        """Scalar evidence returns error envelope."""
        result = await _call_tool(
            server,
            "lithos_edge_upsert",
            {
                "from_id": "node-a",
                "to_id": "node-b",
                "type": "related_to",
                "weight": 0.5,
                "namespace": "default",
                "evidence": "just a string",
            },
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"


class TestEdgeList:
    @pytest.mark.asyncio
    async def test_query_by_from_id(self, server: LithosServer) -> None:
        """Query edges by from_id."""
        await _call_tool(
            server,
            "lithos_edge_upsert",
            {
                "from_id": "src-1",
                "to_id": "tgt-1",
                "type": "related_to",
                "weight": 0.5,
                "namespace": "ns1",
            },
        )
        result = await _call_tool(
            server,
            "lithos_edge_list",
            {"from_id": "src-1"},
        )
        assert len(result["results"]) == 1
        assert result["results"][0]["from_id"] == "src-1"

    @pytest.mark.asyncio
    async def test_query_by_to_id(self, server: LithosServer) -> None:
        """Query edges by to_id."""
        await _call_tool(
            server,
            "lithos_edge_upsert",
            {
                "from_id": "src-2",
                "to_id": "tgt-unique",
                "type": "derived_from",
                "weight": 1.0,
                "namespace": "ns2",
            },
        )
        result = await _call_tool(
            server,
            "lithos_edge_list",
            {"to_id": "tgt-unique"},
        )
        assert len(result["results"]) == 1

    @pytest.mark.asyncio
    async def test_query_by_type(self, server: LithosServer) -> None:
        """Query edges by type."""
        await _call_tool(
            server,
            "lithos_edge_upsert",
            {
                "from_id": "src-3",
                "to_id": "tgt-3",
                "type": "unique_type_test",
                "weight": 0.7,
                "namespace": "ns3",
            },
        )
        result = await _call_tool(
            server,
            "lithos_edge_list",
            {"type": "unique_type_test"},
        )
        assert len(result["results"]) >= 1

    @pytest.mark.asyncio
    async def test_query_by_namespace(self, server: LithosServer) -> None:
        """Query edges by namespace."""
        await _call_tool(
            server,
            "lithos_edge_upsert",
            {
                "from_id": "src-4",
                "to_id": "tgt-4",
                "type": "related_to",
                "weight": 0.5,
                "namespace": "unique_ns_test",
            },
        )
        result = await _call_tool(
            server,
            "lithos_edge_list",
            {"namespace": "unique_ns_test"},
        )
        assert len(result["results"]) == 1

    @pytest.mark.asyncio
    async def test_empty_results(self, server: LithosServer) -> None:
        """Query with no matches returns empty list."""
        result = await _call_tool(
            server,
            "lithos_edge_list",
            {"from_id": "nonexistent-node"},
        )
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_result_has_all_keys(self, server: LithosServer) -> None:
        """Edge dict has all specified keys."""
        await _call_tool(
            server,
            "lithos_edge_upsert",
            {
                "from_id": "src-keys",
                "to_id": "tgt-keys",
                "type": "test_type",
                "weight": 0.5,
                "namespace": "default",
                "provenance_actor": "test-agent",
                "provenance_type": "manual",
                "conflict_state": "none",
            },
        )
        result = await _call_tool(
            server,
            "lithos_edge_list",
            {"from_id": "src-keys"},
        )
        edge = result["results"][0]
        expected_keys = {
            "edge_id",
            "from_id",
            "to_id",
            "type",
            "weight",
            "namespace",
            "created_at",
            "updated_at",
            "provenance_actor",
            "provenance_type",
            "evidence",
            "conflict_state",
        }
        assert set(edge.keys()) == expected_keys


class TestEdgeUpsertEvent:
    @pytest.mark.asyncio
    async def test_publishes_edge_upserted_event(self, server: LithosServer) -> None:
        """lithos_edge_upsert publishes edge.upserted event."""
        from lithos.events import EDGE_UPSERTED

        queue = server.event_bus.subscribe(event_types=[EDGE_UPSERTED])

        await _call_tool(
            server,
            "lithos_edge_upsert",
            {
                "from_id": "ev-src",
                "to_id": "ev-tgt",
                "type": "supports",
                "weight": 0.5,
                "namespace": "default",
            },
        )

        event = await queue.get()
        assert event.type == EDGE_UPSERTED
        assert event.payload["from_id"] == "ev-src"
        assert event.payload["to_id"] == "ev-tgt"
