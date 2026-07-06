"""Shared MCP-boundary test helpers."""

import json
from typing import Any

from lithos.server import LithosServer


async def call_tool(server: LithosServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call an MCP tool through FastMCP dispatch and decode the JSON payload.

    The sole wrapper over the private ``FastMCP._call_tool_mcp`` — a FastMCP
    upgrade breaks one file instead of every conformance suite.
    """
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


def assert_error_envelope(result: dict[str, Any], code: str | None = None) -> None:
    """Assert ``result`` is the canonical error envelope.

    Canonical shape: ``{"status": "error", "code": <str>, "message": <str>}``
    with optional documented supplementary keys, never ``warnings``, and never
    the legacy ``status: "invalid_input"`` dialect.
    """
    assert result["status"] == "error", f"expected status 'error', got {result.get('status')!r}"
    assert isinstance(result.get("code"), str) and result["code"], f"missing code: {result!r}"
    assert isinstance(result.get("message"), str) and result["message"], (
        f"missing message: {result!r}"
    )
    assert "warnings" not in result, f"error envelopes must not carry warnings: {result!r}"
    if code is not None:
        assert result["code"] == code, f"expected code {code!r}, got {result['code']!r}"
