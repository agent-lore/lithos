"""Integration tests for MCP HTTP connectivity to a running server.

Lithos serves both transports on one port (#304): legacy SSE at ``/sse`` and
StreamableHTTP at ``/mcp``. These tests exercise both against the real
``LithosServer.serve_http`` boundary.
"""

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
_streamable_http = pytest.importorskip("mcp.client.streamable_http")
# ``streamable_http_client`` is the current name; older mcp releases only ship
# the deprecated ``streamablehttp_client`` alias. Prefer the new name.
streamablehttp_client = (
    getattr(_streamable_http, "streamable_http_client", None)
    or _streamable_http.streamablehttp_client
)

pytestmark = pytest.mark.integration


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@asynccontextmanager
async def _local_server(temp_dir: Path):
    """Start a local Lithos HTTP server and yield its base URL (no path)."""
    config = LithosConfig(storage=StorageConfig(data_dir=temp_dir))
    config.ensure_directories()
    server = LithosServer(config)
    await server.initialize()

    host = "127.0.0.1"
    port = _find_free_port()
    task = asyncio.create_task(server.serve_http(host=host, port=port))

    base = f"http://{host}:{port}"
    try:
        # Wait until the SSE endpoint is reachable before yielding.
        for _ in range(100):
            try:
                async with sse_client(f"{base}/sse"):
                    break
            except Exception:
                await asyncio.sleep(0.05)
        else:
            raise AssertionError("Timed out waiting for local HTTP server to start")

        yield base
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await server.shutdown()


@asynccontextmanager
async def _resolve_base(temp_dir: Path):
    """Yield the base server URL, honouring LITHOS_MCP_URL for CI.

    ``LITHOS_MCP_URL`` may point at either the base URL or a transport path
    (``/sse`` or ``/mcp``); the trailing transport segment is stripped so both
    endpoints can be derived from the base.
    """
    endpoint = os.environ.get("LITHOS_MCP_URL")
    if endpoint:
        base = endpoint.rstrip("/")
        for suffix in ("/sse", "/mcp"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        yield base
        return

    async with _local_server(temp_dir) as base:
        yield base


@pytest.mark.asyncio
async def test_mcp_sse_lists_tools(temp_dir):
    """Connect to Lithos MCP SSE endpoint and verify tool discovery."""
    async with (
        _resolve_base(temp_dir) as base,
        sse_client(f"{base}/sse") as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await asyncio.wait_for(session.initialize(), timeout=20)
        tools = await asyncio.wait_for(session.list_tools(), timeout=20)

    assert len(tools.tools) >= 20


@pytest.mark.asyncio
async def test_mcp_streamable_http_lists_tools(temp_dir):
    """Connect to Lithos MCP StreamableHTTP endpoint (/mcp) and verify tools.

    Guards the #304 acceptance criteria: POST /mcp speaks StreamableHTTP and
    exposes the same tool set as /sse, with no proxy in between.
    """
    async with (
        _resolve_base(temp_dir) as base,
        streamablehttp_client(f"{base}/mcp") as (reader, writer, _get_session_id),
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
        _resolve_base(temp_dir) as base,
        sse_client(f"{base}/sse") as (reader, writer),
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
