"""Tests for US-004: lithos_conflict_resolve MCP tool.

Unit tests that exercise conflict resolution logic directly via the server.
"""

import json
from typing import Any

import pytest

from lithos.config import LithosConfig
from lithos.server import LithosServer


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


@pytest.fixture
async def server_with_notes(test_config: LithosConfig) -> LithosServer:
    """Server with two notes and a contradiction edge between them."""
    srv = LithosServer(test_config)
    await srv.initialize()

    # Create two notes
    r1 = await _call_tool(
        srv,
        "lithos_write",
        {"title": "Note Alpha", "content": "Alpha says X is true.", "agent": "agent-a"},
    )
    r2 = await _call_tool(
        srv,
        "lithos_write",
        {"title": "Note Beta", "content": "Beta says X is false.", "agent": "agent-b"},
    )
    srv._note_alpha_id = r1["id"]  # type: ignore[attr-defined]
    srv._note_beta_id = r2["id"]  # type: ignore[attr-defined]

    # Create a contradiction edge between them
    edge_result = await _call_tool(
        srv,
        "lithos_edge_upsert",
        {
            "from_id": r1["id"],
            "to_id": r2["id"],
            "type": "contradicts",
            "weight": 1.0,
            "namespace": "default",
        },
    )
    srv._contradiction_edge_id = edge_result["edge_id"]  # type: ignore[attr-defined]

    # Also create a non-contradiction edge for negative testing
    non_c = await _call_tool(
        srv,
        "lithos_edge_upsert",
        {
            "from_id": r1["id"],
            "to_id": r2["id"],
            "type": "related_to",
            "weight": 0.5,
            "namespace": "default",
        },
    )
    srv._related_edge_id = non_c["edge_id"]  # type: ignore[attr-defined]

    yield srv  # type: ignore[misc]
    srv.stop_file_watcher()


class TestConflictResolveValidResolutions:
    """Resolve with each valid resolution state."""

    @pytest.mark.asyncio
    async def test_resolve_accepted_dual(self, server_with_notes: LithosServer) -> None:
        srv = server_with_notes
        result = await _call_tool(
            srv,
            "lithos_conflict_resolve",
            {
                "edge_id": srv._contradiction_edge_id,  # type: ignore[attr-defined]
                "resolution": "accepted_dual",
                "resolver": "human-reviewer",
            },
        )
        assert result["status"] == "ok"
        assert result["edge_id"] == srv._contradiction_edge_id  # type: ignore[attr-defined]
        assert result["conflict_state"] == "accepted_dual"

    @pytest.mark.asyncio
    async def test_resolve_refuted(self, server_with_notes: LithosServer) -> None:
        srv = server_with_notes
        result = await _call_tool(
            srv,
            "lithos_conflict_resolve",
            {
                "edge_id": srv._contradiction_edge_id,  # type: ignore[attr-defined]
                "resolution": "refuted",
                "resolver": "agent-c",
            },
        )
        assert result["status"] == "ok"
        assert result["conflict_state"] == "refuted"

    @pytest.mark.asyncio
    async def test_resolve_merged(self, server_with_notes: LithosServer) -> None:
        srv = server_with_notes
        result = await _call_tool(
            srv,
            "lithos_conflict_resolve",
            {
                "edge_id": srv._contradiction_edge_id,  # type: ignore[attr-defined]
                "resolution": "merged",
                "resolver": "agent-c",
            },
        )
        assert result["status"] == "ok"
        assert result["conflict_state"] == "merged"

    @pytest.mark.asyncio
    async def test_resolve_superseded(self, server_with_notes: LithosServer) -> None:
        srv = server_with_notes
        alpha_id = srv._note_alpha_id  # type: ignore[attr-defined]
        result = await _call_tool(
            srv,
            "lithos_conflict_resolve",
            {
                "edge_id": srv._contradiction_edge_id,  # type: ignore[attr-defined]
                "resolution": "superseded",
                "resolver": "agent-c",
                "winner_id": alpha_id,
            },
        )
        assert result["status"] == "ok"
        assert result["conflict_state"] == "superseded"

        # Verify supersedes was set on the winner note
        doc, _ = await srv.knowledge.read(id=alpha_id)
        beta_id = srv._note_beta_id  # type: ignore[attr-defined]
        assert doc.metadata.supersedes == beta_id


class TestConflictResolveInvalidResolution:
    """Reject invalid resolution string."""

    @pytest.mark.asyncio
    async def test_rejects_invalid_resolution(self, server_with_notes: LithosServer) -> None:
        srv = server_with_notes
        result = await _call_tool(
            srv,
            "lithos_conflict_resolve",
            {
                "edge_id": srv._contradiction_edge_id,  # type: ignore[attr-defined]
                "resolution": "ignored",
                "resolver": "agent-c",
            },
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"
        assert "ignored" in result["message"]


class TestConflictResolveNonContradiction:
    """Reject non-contradiction edge."""

    @pytest.mark.asyncio
    async def test_rejects_non_contradiction_edge(self, server_with_notes: LithosServer) -> None:
        srv = server_with_notes
        result = await _call_tool(
            srv,
            "lithos_conflict_resolve",
            {
                "edge_id": srv._related_edge_id,  # type: ignore[attr-defined]
                "resolution": "accepted_dual",
                "resolver": "agent-c",
            },
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"
        assert "contradicts" in result["message"]


class TestConflictResolveSupersededValidation:
    """Reject superseded resolution without valid winner_id."""

    @pytest.mark.asyncio
    async def test_rejects_superseded_without_winner(self, server_with_notes: LithosServer) -> None:
        srv = server_with_notes
        result = await _call_tool(
            srv,
            "lithos_conflict_resolve",
            {
                "edge_id": srv._contradiction_edge_id,  # type: ignore[attr-defined]
                "resolution": "superseded",
                "resolver": "agent-c",
            },
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"
        assert "winner_id" in result["message"]

    @pytest.mark.asyncio
    async def test_rejects_superseded_with_invalid_winner(
        self, server_with_notes: LithosServer
    ) -> None:
        srv = server_with_notes
        result = await _call_tool(
            srv,
            "lithos_conflict_resolve",
            {
                "edge_id": srv._contradiction_edge_id,  # type: ignore[attr-defined]
                "resolution": "superseded",
                "resolver": "agent-c",
                "winner_id": "some-random-id",
            },
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"
        assert "winner_id" in result["message"]


class TestConflictResolveUpdateFailure:
    """Error when edge update fails (e.g. edge deleted between get and update)."""

    @pytest.mark.asyncio
    async def test_returns_error_when_update_fails(self, server_with_notes: LithosServer) -> None:
        from unittest.mock import AsyncMock, patch

        srv = server_with_notes
        edge_id = srv._contradiction_edge_id  # type: ignore[attr-defined]

        # Simulate the edge disappearing between get_edge and update
        with patch.object(
            srv.edge_store,
            "update_conflict_resolution",
            new_callable=AsyncMock,
            return_value=False,
        ):
            result = await _call_tool(
                srv,
                "lithos_conflict_resolve",
                {
                    "edge_id": edge_id,
                    "resolution": "accepted_dual",
                    "resolver": "agent-c",
                },
            )
        assert result["status"] == "error"
        assert result["code"] == "update_failed"

    @pytest.mark.asyncio
    async def test_superseded_does_not_mutate_note_when_update_fails(
        self, server_with_notes: LithosServer
    ) -> None:
        from unittest.mock import AsyncMock, patch

        srv = server_with_notes
        edge_id = srv._contradiction_edge_id  # type: ignore[attr-defined]
        alpha_id = srv._note_alpha_id  # type: ignore[attr-defined]

        # Read the note before to confirm no supersedes
        doc_before, _ = await srv.knowledge.read(id=alpha_id)
        assert doc_before.metadata.supersedes is None

        with patch.object(
            srv.edge_store,
            "update_conflict_resolution",
            new_callable=AsyncMock,
            return_value=False,
        ):
            result = await _call_tool(
                srv,
                "lithos_conflict_resolve",
                {
                    "edge_id": edge_id,
                    "resolution": "superseded",
                    "resolver": "agent-c",
                    "winner_id": alpha_id,
                },
            )
        assert result["status"] == "error"
        assert result["code"] == "update_failed"

        # Verify the note was NOT mutated
        doc_after, _ = await srv.knowledge.read(id=alpha_id)
        assert doc_after.metadata.supersedes is None


class TestConflictResolveEdgeNotFound:
    """Error when edge not found."""

    @pytest.mark.asyncio
    async def test_rejects_missing_edge(self, server_with_notes: LithosServer) -> None:
        srv = server_with_notes
        result = await _call_tool(
            srv,
            "lithos_conflict_resolve",
            {
                "edge_id": "edge_nonexistent",
                "resolution": "accepted_dual",
                "resolver": "agent-c",
            },
        )
        assert result["status"] == "error"
        assert result["code"] == "not_found"
