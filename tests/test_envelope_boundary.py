"""Server-boundary regression tests for exact error-envelope payloads.

The envelope constructors are unit-tested in ``test_envelopes.py``; these
tests pin the *wire* result — exact keys, key order, and values — for
representative handlers that adopted the constructors, proving the adoption
changed no bytes at the MCP boundary.
"""

import pytest

from lithos.server import LithosServer
from tests.helpers import call_tool

pytestmark = pytest.mark.integration


class TestCanonicalEnvelopeAtBoundary:
    async def test_task_get_not_found_exact_payload_and_key_order(self, server: LithosServer):
        result = await call_tool(server, "lithos_task_get", {"task_id": "ghost"})

        assert result == {
            "status": "error",
            "code": "task_not_found",
            "message": "Task 'ghost' not found.",
        }
        assert list(result) == ["status", "code", "message"]

    async def test_task_create_invalid_type_maps_coordination_error(self, server: LithosServer):
        result = await call_tool(
            server,
            "lithos_task_create",
            {"title": "t", "agent": "a", "task_type": "bogus"},
        )

        assert result["status"] == "error"
        assert result["code"] == "invalid_task_type"
        assert list(result) == ["status", "code", "message"]

    async def test_task_ready_invalid_limit_exact_payload(self, server: LithosServer):
        result = await call_tool(server, "lithos_task_ready", {"limit": 0})

        assert result == {
            "status": "error",
            "code": "invalid_input",
            "message": "limit must be >= 1, got 0.",
        }
        assert list(result) == ["status", "code", "message"]
