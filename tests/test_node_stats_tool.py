"""Tests for lithos_node_stats MCP tool (US-008)."""

from typing import Any

import pytest
import pytest_asyncio

from lithos.config import LithosConfig
from lithos.server import LithosServer

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def srv(test_config: LithosConfig) -> LithosServer:
    server = LithosServer(test_config)
    await server.initialize()
    yield server  # type: ignore[misc]
    server.stop_file_watcher()


async def _call(server: LithosServer, tool_name: str, **kwargs: Any) -> dict[str, Any]:
    tool = await server.mcp.get_tool(tool_name)
    return await tool.fn(**kwargs)


class TestNodeStatsTool:
    """lithos_node_stats MCP tool tests."""

    @pytest.mark.asyncio
    async def test_stats_for_node_with_retrieval_history(self, srv: LithosServer) -> None:
        """Query stats for node with retrieval history."""
        doc = (
            await srv.knowledge.create(title="Stat Note", content="Content.", agent="agent")
        ).document
        assert doc is not None

        # Simulate some stats
        await srv.stats_store.increment_cited(doc.id)
        await srv.stats_store.increment_cited(doc.id)
        await srv.stats_store.update_salience(doc.id, 0.04)
        await srv.stats_store.increment_ignored(doc.id)

        result = await _call(srv, "lithos_node_stats", node_id=doc.id)
        assert result["node_id"] == doc.id
        assert result["cited_count"] == 2
        assert result["ignored_count"] == 1
        assert isinstance(result["salience"], float)
        assert abs(result["salience"] - 0.54) < 0.001
        assert result["retrieval_count"] == 0
        assert result["misleading_count"] == 0
        assert result["decay_rate"] == 0.0
        assert result["spaced_rep_strength"] == 0.0
        assert result["last_decay_applied_at"] is None

    @pytest.mark.asyncio
    async def test_stats_for_node_with_no_stats_row(self, srv: LithosServer) -> None:
        """Query stats for node with no stats row — verify defaults."""
        doc = (
            await srv.knowledge.create(title="Fresh Note", content="Content.", agent="agent")
        ).document
        assert doc is not None

        result = await _call(srv, "lithos_node_stats", node_id=doc.id)
        assert result["node_id"] == doc.id
        assert result["salience"] == 0.5
        assert result["retrieval_count"] == 0
        assert result["cited_count"] == 0
        assert result["last_retrieved_at"] is None
        assert result["last_used_at"] is None
        assert result["ignored_count"] == 0
        assert result["misleading_count"] == 0
        assert result["decay_rate"] == 0.0
        assert result["spaced_rep_strength"] == 0.0
        assert result["last_decay_applied_at"] is None

    @pytest.mark.asyncio
    async def test_invalid_node_id_returns_error(self, srv: LithosServer) -> None:
        """Query with invalid node_id — verify error."""
        result = await _call(
            srv, "lithos_node_stats", node_id="00000000-0000-0000-0000-000000000000"
        )
        assert result["status"] == "error"
        assert result["code"] == "doc_not_found"
        assert "00000000-0000-0000-0000-000000000000" in result["message"]
