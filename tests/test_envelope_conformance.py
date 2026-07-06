"""Golden conformance tests for the canonical MCP error-envelope contract.

Authored with the envelope normalization (PR 2 of the tool-handler
extraction) and kept green, unmodified, through the handler moves (PRs 3-5)
— this suite is the move-purity guardrail.

Contract under test:

* Every tool failure is ``{"status": "error", "code", "message"}``;
  validation failures carry ``code: "invalid_input"``.
* Error envelopes never carry ``warnings`` and never the legacy
  ``status: "invalid_input"`` dialect.
* Actionable write outcomes keep their own top-level status —
  ``version_conflict`` (with ``current_version``), ``duplicate``,
  ``slug_collision``, ``path_collision`` — they are not errors.
"""

from typing import Any

import pytest

from lithos.errors import SearchBackendError
from lithos.server import LithosServer
from tests.helpers import assert_error_envelope, call_tool

pytestmark = pytest.mark.integration


EXPECTED_TOOLS = [
    "lithos_agent_info",
    "lithos_agent_list",
    "lithos_agent_register",
    "lithos_cache_lookup",
    "lithos_conflict_resolve",
    "lithos_delete",
    "lithos_edge_list",
    "lithos_edge_upsert",
    "lithos_finding_list",
    "lithos_finding_post",
    "lithos_list",
    "lithos_node_stats",
    "lithos_note_update",
    "lithos_read",
    "lithos_related",
    "lithos_retrieve",
    "lithos_search",
    "lithos_stats",
    "lithos_tags",
    "lithos_task_blocked",
    "lithos_task_cancel",
    "lithos_task_children",
    "lithos_task_claim",
    "lithos_task_complete",
    "lithos_task_create",
    "lithos_task_edge_list",
    "lithos_task_edge_upsert",
    "lithos_task_get",
    "lithos_task_list",
    "lithos_task_ready",
    "lithos_task_release",
    "lithos_task_renew",
    "lithos_task_reopen",
    "lithos_task_spawn",
    "lithos_task_status",
    "lithos_task_update",
    "lithos_write",
]


class TestToolSurfaceSnapshot:
    """The registered tool set is frozen; the moves must not drift it."""

    async def test_tool_names_match_snapshot(self, server: LithosServer):
        tools = await server.mcp.list_tools()
        assert sorted(tool.name for tool in tools) == EXPECTED_TOOLS

    async def test_every_tool_has_description_and_schema(self, server: LithosServer):
        tools = await server.mcp.list_tools()
        for tool in tools:
            assert tool.description, f"{tool.name} has no description"
            assert tool.parameters, f"{tool.name} has no parameter schema"


# (tool, arguments, expected code) — one cheap, deterministic failure per
# error family. Exercised through FastMCP dispatch so JSON serialisation is
# part of what's pinned.
VALIDATION_CASES: list[tuple[str, dict[str, Any], str]] = [
    # write family: boundary validation
    (
        "lithos_write",
        {"title": "t", "content": "c", "agent": "a", "ttl_hours": 1, "expires_at": "2030-01-01"},
        "invalid_input",
    ),
    (
        "lithos_write",
        {"title": "t", "content": "c", "agent": "a", "ttl_hours": -1},
        "invalid_input",
    ),
    (
        "lithos_write",
        {"title": "t", "content": "c", "agent": "a", "access_scope": "bogus"},
        "invalid_input",
    ),
    (
        "lithos_write",
        {"title": "t", "content": "c", "agent": "a", "metadata": {"title": "reserved"}},
        "invalid_input",
    ),
    (
        "lithos_note_update",
        {"id": "00000000-0000-0000-0000-000000000000", "agent": "a"},
        "invalid_input",
    ),
    # metadata_match validation (previously the legacy invalid_input dialect)
    ("lithos_list", {"metadata_match": {"k": ["not-a-scalar"]}}, "invalid_input"),
    ("lithos_task_list", {"metadata_match": {"k": ["not-a-scalar"]}}, "invalid_input"),
    ("lithos_task_ready", {"metadata_match": {"k": ["not-a-scalar"]}}, "invalid_input"),
    ("lithos_task_blocked", {"metadata_match": {"k": ["not-a-scalar"]}}, "invalid_input"),
    # datetime filters: unparseable values are boundary-validated, never ToolErrors
    ("lithos_list", {"since": "not-a-date"}, "invalid_input"),
    ("lithos_agent_list", {"active_since": "not-a-date"}, "invalid_input"),
    ("lithos_finding_list", {"task_id": "any-task", "since": "not-a-date"}, "invalid_input"),
    # not-found family
    (
        "lithos_read",
        {"id": "00000000-0000-0000-0000-000000000000", "agent_id": "a"},
        "doc_not_found",
    ),
    ("lithos_task_get", {"task_id": "no-such-task"}, "task_not_found"),
    # edge tools (already canonical before normalization)
    (
        "lithos_edge_upsert",
        {"from_id": "a", "to_id": "b", "type": "related_to", "weight": 0.5, "namespace": ""},
        "invalid_input",
    ),
    # CoordinationError mapping through the seam
    (
        "lithos_task_create",
        {"title": "t", "agent": "a", "metadata": {"depends_on": []}},
        "invalid_metadata_key",
    ),
    # search mode validation
    ("lithos_search", {"query": "q", "mode": "bogus"}, "invalid_mode"),
]


class TestCanonicalErrorEnvelopes:
    @pytest.mark.parametrize(("tool", "arguments", "code"), VALIDATION_CASES)
    async def test_failure_is_canonical(
        self, server: LithosServer, tool: str, arguments: dict[str, Any], code: str
    ):
        result = await call_tool(server, tool, arguments)
        assert_error_envelope(result, code=code)

    async def test_coordination_error_via_self_edge(self, server: LithosServer):
        created = await call_tool(server, "lithos_task_create", {"title": "t", "agent": "a"})
        task_id = created["task_id"]
        result = await call_tool(
            server,
            "lithos_task_edge_upsert",
            {"from_task_id": task_id, "to_task_id": task_id, "type": "blocks", "agent": "a"},
        )
        assert_error_envelope(result, code="self_edge")

    async def test_note_update_unknown_id_is_note_not_found(self, server: LithosServer):
        result = await call_tool(
            server,
            "lithos_note_update",
            {"id": "00000000-0000-0000-0000-000000000000", "agent": "a", "title": "new"},
        )
        assert_error_envelope(result, code="note_not_found")

    async def test_search_backend_error_is_canonical(
        self, server: LithosServer, monkeypatch: pytest.MonkeyPatch
    ):
        def _boom(*args: Any, **kwargs: Any):
            raise SearchBackendError("backends down", {"tantivy": RuntimeError("x")})

        monkeypatch.setattr(server.search, "full_text_search", _boom)
        result = await call_tool(server, "lithos_list", {"content_query": "q"})
        assert_error_envelope(result, code="search_backend_error")


class TestVersionConflictStaysAnOutcome:
    """Read-merge-write retry loops branch on status == "version_conflict"."""

    async def test_golden_version_conflict_shape(self, server: LithosServer):
        created = await call_tool(
            server, "lithos_write", {"title": "vc doc", "content": "v1", "agent": "a"}
        )
        assert created["status"] == "created"

        result = await call_tool(
            server,
            "lithos_write",
            {
                "id": created["id"],
                "title": "vc doc",
                "content": "v2",
                "agent": "a",
                "expected_version": 999,
            },
        )
        assert result["status"] == "version_conflict"
        assert result["current_version"] == created["version"]
        assert isinstance(result["message"], str) and result["message"]
        assert isinstance(result["warnings"], list)
        assert "code" not in result


class TestGoldenInvalidInputEnvelope:
    """Exact-dict pin of the canonical validation envelope through dispatch."""

    async def test_write_ttl_and_expires_mutual_exclusion(self, server: LithosServer):
        result = await call_tool(
            server,
            "lithos_write",
            {
                "title": "t",
                "content": "c",
                "agent": "a",
                "ttl_hours": 1,
                "expires_at": "2030-01-01T00:00:00Z",
            },
        )
        assert result == {
            "status": "error",
            "code": "invalid_input",
            "message": "Provide either ttl_hours or expires_at, not both.",
        }
        assert list(result) == ["status", "code", "message"]

    async def test_task_get_not_found_exact(self, server: LithosServer):
        result = await call_tool(server, "lithos_task_get", {"task_id": "ghost"})
        assert result == {
            "status": "error",
            "code": "task_not_found",
            "message": "Task 'ghost' not found.",
        }
