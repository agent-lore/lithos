"""Integration test for MCP-over-SSE connectivity to a running server."""

import asyncio
import json
import os

import pytest

mcp = pytest.importorskip("mcp", reason="mcp package is only installed in integration CI job")
ClientSession = mcp.ClientSession
sse_client = pytest.importorskip("mcp.client.sse").sse_client

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_mcp_sse_lists_tools():
    """Connect to running Lithos MCP SSE endpoint and verify tool discovery."""
    endpoint = os.environ.get("LITHOS_MCP_URL")
    if not endpoint:
        pytest.skip("Set LITHOS_MCP_URL to run SSE integration test")

    async with sse_client(endpoint) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await asyncio.wait_for(session.initialize(), timeout=20)
            tools = await asyncio.wait_for(session.list_tools(), timeout=20)

    assert len(tools.tools) >= 20


def _decode_call_result(result) -> dict:
    """Decode MCP call_tool result payload."""
    blocks = getattr(result, "content", [])
    if blocks and getattr(blocks[0], "text", None):
        return json.loads(blocks[0].text)
    raise AssertionError(f"Unexpected MCP call result: {result!r}")


@pytest.mark.asyncio
async def test_mcp_sse_remote_tool_roundtrip():
    """Run an end-to-end tool workflow through the remote SSE MCP boundary."""
    endpoint = os.environ.get("LITHOS_MCP_URL")
    if not endpoint:
        pytest.skip("Set LITHOS_MCP_URL to run SSE integration test")

    async with sse_client(endpoint) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await asyncio.wait_for(session.initialize(), timeout=20)

            write = await asyncio.wait_for(
                session.call_tool(
                    "lithos_write",
                    {
                        "title": "SSE Roundtrip Doc",
                        "content": "Remote roundtrip content over SSE MCP boundary.",
                        "agent": "sse-agent",
                        "tags": ["sse", "roundtrip"],
                    },
                ),
                timeout=30,
            )
            write_payload = _decode_call_result(write)
            doc_id = write_payload["id"]

            read = await asyncio.wait_for(
                session.call_tool("lithos_read", {"id": doc_id}),
                timeout=30,
            )
            read_payload = _decode_call_result(read)
            assert read_payload["id"] == doc_id
            assert read_payload["title"] == "SSE Roundtrip Doc"

            search = await asyncio.wait_for(
                session.call_tool("lithos_search", {"query": "roundtrip content over SSE", "limit": 10}),
                timeout=30,
            )
            search_payload = _decode_call_result(search)
            assert any(item["id"] == doc_id for item in search_payload["results"])

            task = await asyncio.wait_for(
                session.call_tool(
                    "lithos_task_create",
                    {"title": "SSE Roundtrip Task", "agent": "sse-agent"},
                ),
                timeout=30,
            )
            task_payload = _decode_call_result(task)
            task_id = task_payload["task_id"]

            finding = await asyncio.wait_for(
                session.call_tool(
                    "lithos_finding_post",
                    {
                        "task_id": task_id,
                        "agent": "sse-agent",
                        "summary": "SSE finding summary",
                        "knowledge_id": doc_id,
                    },
                ),
                timeout=30,
            )
            finding_payload = _decode_call_result(finding)
            assert finding_payload["finding_id"]

            finding_list = await asyncio.wait_for(
                session.call_tool("lithos_finding_list", {"task_id": task_id}),
                timeout=30,
            )
            finding_list_payload = _decode_call_result(finding_list)
            assert any(
                f["summary"] == "SSE finding summary" and f["knowledge_id"] == doc_id
                for f in finding_list_payload["findings"]
            )
