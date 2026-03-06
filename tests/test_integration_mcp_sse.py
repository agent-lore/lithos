"""Integration test for MCP-over-SSE connectivity to a running server."""

import asyncio
import json
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from lithos.config import LithosConfig, StorageConfig
from lithos.server import LithosServer

mcp = pytest.importorskip("mcp", reason="mcp package is only installed in integration CI job")
ClientSession = mcp.ClientSession
sse_client = pytest.importorskip("mcp.client.sse").sse_client

pytestmark = pytest.mark.integration


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@asynccontextmanager
async def _local_sse_endpoint(temp_dir: Path):
    config = LithosConfig(storage=StorageConfig(data_dir=temp_dir))
    config.ensure_directories()
    server = LithosServer(config)
    await server.initialize()

    host = "127.0.0.1"
    port = _find_free_port()
    task = asyncio.create_task(
        server.mcp.run_http_async(
            transport="sse",
            host=host,
            port=port,
            path="/sse",
            show_banner=False,
        )
    )

    endpoint = f"http://{host}:{port}/sse"
    try:
        # Wait until endpoint is reachable.
        for _ in range(100):
            try:
                async with sse_client(endpoint):
                    break
            except Exception:
                await asyncio.sleep(0.05)
        else:
            raise AssertionError("Timed out waiting for local SSE endpoint to start")

        yield endpoint
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.stop_file_watcher()


@asynccontextmanager
async def _resolve_endpoint(temp_dir: Path):
    endpoint = os.environ.get("LITHOS_MCP_URL")
    if endpoint:
        yield endpoint
        return

    async with _local_sse_endpoint(temp_dir) as local_endpoint:
        yield local_endpoint


@pytest.mark.asyncio
async def test_mcp_sse_lists_tools(temp_dir):
    """Connect to Lithos MCP SSE endpoint and verify tool discovery."""
    async with (
        _resolve_endpoint(temp_dir) as endpoint,
        sse_client(endpoint) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
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
async def test_mcp_sse_remote_tool_roundtrip(temp_dir):
    """Run an end-to-end tool workflow through the SSE MCP boundary."""
    async with (
        _resolve_endpoint(temp_dir) as endpoint,
        sse_client(endpoint) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
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
            session.call_tool(
                "lithos_search", {"query": "roundtrip content over SSE", "limit": 10}
            ),
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
