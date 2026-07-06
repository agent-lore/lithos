"""Server-boundary regression tests for exact error-envelope payloads.

The envelope constructors are unit-tested in ``test_envelopes.py``; these
tests pin the *wire* result — exact keys, key order, and values — for
representative handlers that adopted the constructors, proving the adoption
changed no bytes at the MCP boundary.
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


class TestCanonicalEnvelopeAtBoundary:
    async def test_task_get_not_found_exact_payload_and_key_order(self, server: LithosServer):
        result = await _call_tool(server, "lithos_task_get", {"task_id": "ghost"})

        assert result == {
            "status": "error",
            "code": "task_not_found",
            "message": "Task 'ghost' not found.",
        }
        assert list(result) == ["status", "code", "message"]

    async def test_task_create_invalid_type_maps_coordination_error(self, server: LithosServer):
        result = await _call_tool(
            server,
            "lithos_task_create",
            {"title": "t", "agent": "a", "task_type": "bogus"},
        )

        assert result["status"] == "error"
        assert result["code"] == "invalid_task_type"
        assert list(result) == ["status", "code", "message"]

    async def test_task_ready_invalid_limit_exact_payload(self, server: LithosServer):
        result = await _call_tool(server, "lithos_task_ready", {"limit": 0})

        assert result == {
            "status": "error",
            "code": "invalid_input",
            "message": "limit must be >= 1, got 0.",
        }
        assert list(result) == ["status", "code", "message"]
