"""Tests for US-002: lithos_write accepts optional LCMA fields."""

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


class TestLithosWriteCreateWithLcmaFields:
    """Test creating notes with LCMA fields via lithos_write."""

    @pytest.mark.asyncio
    async def test_create_with_lcma_fields(self, server: LithosServer) -> None:
        """Create a note with explicit LCMA fields and verify they persist."""
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "LCMA Note",
                "content": "Content with LCMA",
                "agent": "test-agent",
                "schema_version": 2,
                "namespace": "research",
                "access_scope": "task",
                "note_type": "hypothesis",
                "status": "active",
                "summaries": {"short": "A short summary", "long": "A long summary"},
                "source_task": "task-123",
            },
        )
        assert result["status"] == "created"
        doc_id = result["id"]

        # Read back and verify LCMA fields
        read_result = await _call_tool(
            server, "lithos_read", {"id": doc_id, "agent_id": "test-agent"}
        )
        meta = read_result["metadata"]
        assert meta["schema_version"] == 2
        assert meta["namespace"] == "research"
        assert meta["access_scope"] == "task"
        assert meta["note_type"] == "hypothesis"
        assert meta["status"] == "active"
        assert meta["summaries"] == {"short": "A short summary", "long": "A long summary"}

    @pytest.mark.asyncio
    async def test_create_without_lcma_params_gets_defaults(self, server: LithosServer) -> None:
        """Create without LCMA params should write defaults."""
        result = await _call_tool(
            server,
            "lithos_write",
            {"title": "Plain Note", "content": "No LCMA", "agent": "test-agent"},
        )
        assert result["status"] == "created"

        read_result = await _call_tool(
            server, "lithos_read", {"id": result["id"], "agent_id": "test-agent"}
        )
        meta = read_result["metadata"]
        assert meta["schema_version"] == 1
        assert meta["access_scope"] == "shared"
        assert meta["note_type"] == "observation"
        assert meta["status"] == "active"

    @pytest.mark.asyncio
    async def test_create_summaries_persists_in_yaml(self, server: LithosServer) -> None:
        """Summaries dict persists through YAML frontmatter."""
        summaries = {"short": "Brief", "long": "Detailed explanation"}
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Summary Note",
                "content": "Has summaries",
                "agent": "test-agent",
                "summaries": summaries,
            },
        )
        assert result["status"] == "created"

        read_result = await _call_tool(
            server, "lithos_read", {"id": result["id"], "agent_id": "test-agent"}
        )
        assert read_result["metadata"]["summaries"] == summaries


class TestLithosWriteUpdateWithLcmaFields:
    """Test updating notes with LCMA fields via lithos_write."""

    @pytest.mark.asyncio
    async def test_update_preserves_existing_lcma_values(self, server: LithosServer) -> None:
        """Omitting LCMA params on update preserves existing values."""
        create = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Update Test",
                "content": "Original",
                "agent": "test-agent",
                "note_type": "hypothesis",
                "access_scope": "shared",
                "summaries": {"short": "s"},
            },
        )
        doc_id = create["id"]

        # Update content only — LCMA params omitted
        update = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Update Test",
                "content": "Updated content",
                "agent": "test-agent",
                "id": doc_id,
            },
        )
        assert update["status"] == "updated"

        read = await _call_tool(server, "lithos_read", {"id": doc_id, "agent_id": "test-agent"})
        meta = read["metadata"]
        assert meta["note_type"] == "hypothesis"
        assert meta["access_scope"] == "shared"
        assert meta["summaries"] == {"short": "s"}
        assert meta["schema_version"] == 1

    @pytest.mark.asyncio
    async def test_update_pre_lcma_note_writes_defaults_except_namespace(
        self, server: LithosServer
    ) -> None:
        """Updating a pre-LCMA note writes defaults for schema_version, access_scope,
        note_type, status — but NOT namespace (derived at read time)."""
        # Create a note via the manager to avoid lithos_write defaults
        doc = await server.knowledge.create(title="Pre LCMA", content="Old note", agent="old-agent")
        assert doc.document is not None
        doc_id = doc.document.id

        # Manually clear LCMA fields to simulate pre-LCMA note
        raw_doc, _ = await server.knowledge.read(id=doc_id)
        raw_doc.metadata.schema_version = None
        raw_doc.metadata.access_scope = None
        raw_doc.metadata.note_type = None
        raw_doc.metadata.status = None

        # Update via lithos_write without LCMA params
        update = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Pre LCMA",
                "content": "Updated old note",
                "agent": "new-agent",
                "id": doc_id,
            },
        )
        assert update["status"] == "updated"

        read = await _call_tool(server, "lithos_read", {"id": doc_id, "agent_id": "new-agent"})
        meta = read["metadata"]
        assert meta["schema_version"] == 1
        assert meta["access_scope"] == "shared"
        assert meta["note_type"] == "observation"
        assert meta["status"] == "active"

    @pytest.mark.asyncio
    async def test_update_source_task_forwarded(self, server: LithosServer) -> None:
        """source_task on update sets metadata.source."""
        create = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Task Note",
                "content": "Original",
                "agent": "test-agent",
                "source_task": "task-1",
            },
        )
        doc_id = create["id"]

        # Update with new source_task
        await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Task Note",
                "content": "Updated",
                "agent": "test-agent",
                "id": doc_id,
                "source_task": "task-2",
            },
        )

        read = await _call_tool(server, "lithos_read", {"id": doc_id, "agent_id": "test-agent"})
        assert read["metadata"]["source"] == "task-2"

    @pytest.mark.asyncio
    async def test_update_omitted_source_task_preserves_existing(
        self, server: LithosServer
    ) -> None:
        """Omitting source_task on update preserves existing metadata.source."""
        create = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Preserve Source",
                "content": "Content",
                "agent": "test-agent",
                "source_task": "original-task",
            },
        )
        doc_id = create["id"]

        await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Preserve Source",
                "content": "New content",
                "agent": "test-agent",
                "id": doc_id,
            },
        )

        read = await _call_tool(server, "lithos_read", {"id": doc_id, "agent_id": "test-agent"})
        assert read["metadata"]["source"] == "original-task"


class TestLithosWriteEnumValidation:
    """Test LCMA enum validation."""

    @pytest.mark.asyncio
    async def test_invalid_access_scope_rejected(self, server: LithosServer) -> None:
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Bad Scope",
                "content": "Content",
                "agent": "test-agent",
                "access_scope": "invalid_scope",
            },
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"
        assert "access_scope" in result["message"]

    @pytest.mark.asyncio
    async def test_invalid_note_type_rejected(self, server: LithosServer) -> None:
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Bad Type",
                "content": "Content",
                "agent": "test-agent",
                "note_type": "invalid_type",
            },
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"
        assert "note_type" in result["message"]

    @pytest.mark.asyncio
    async def test_invalid_status_rejected(self, server: LithosServer) -> None:
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Bad Status",
                "content": "Content",
                "agent": "test-agent",
                "status": "deleted",
            },
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"
        assert "status" in result["message"]

    @pytest.mark.asyncio
    async def test_invalid_summaries_unknown_key(self, server: LithosServer) -> None:
        """summaries rejects keys other than 'short' and 'long'."""
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Bad Summaries",
                "content": "Content",
                "agent": "test-agent",
                "summaries": {"short": "ok", "medium": "disallowed"},
            },
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"
        assert "summaries" in result["message"]
        assert "medium" in result["message"]

    @pytest.mark.asyncio
    async def test_invalid_summaries_non_string_value(self, server: LithosServer) -> None:
        """summaries values must be strings."""
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Bad Summaries",
                "content": "Content",
                "agent": "test-agent",
                "summaries": {"short": 42},
            },
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"
        assert "summaries" in result["message"]

    @pytest.mark.asyncio
    async def test_valid_summaries_partial_accepted(self, server: LithosServer) -> None:
        """summaries with only one of short/long is still valid."""
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Partial Summaries",
                "content": "Content",
                "agent": "test-agent",
                "summaries": {"short": "Just the short one"},
            },
        )
        assert result["status"] == "created"


class TestLithosWriteTaskScopeInvariant:
    """Test task-scope access_scope enforcement."""

    @pytest.mark.asyncio
    async def test_task_scope_requires_source_task_on_create(self, server: LithosServer) -> None:
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Task Scope No Task",
                "content": "Content",
                "agent": "test-agent",
                "access_scope": "task",
            },
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"
        assert "source_task" in result["message"]

    @pytest.mark.asyncio
    async def test_task_scope_with_source_task_succeeds(self, server: LithosServer) -> None:
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Task Scope With Task",
                "content": "Content",
                "agent": "test-agent",
                "access_scope": "task",
                "source_task": "task-abc",
            },
        )
        assert result["status"] == "created"

    @pytest.mark.asyncio
    async def test_task_scope_update_existing_source_ok(self, server: LithosServer) -> None:
        """Update with task scope succeeds when note already has source."""
        create = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Has Source",
                "content": "Content",
                "agent": "test-agent",
                "source_task": "task-xyz",
            },
        )
        doc_id = create["id"]

        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Has Source",
                "content": "Updated",
                "agent": "test-agent",
                "id": doc_id,
                "access_scope": "task",
            },
        )
        assert result["status"] == "updated"

    @pytest.mark.asyncio
    async def test_task_scope_update_no_source_rejected(self, server: LithosServer) -> None:
        """Update with task scope fails when note has no source and no source_task provided."""
        create = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "No Source Note",
                "content": "Content",
                "agent": "test-agent",
            },
        )
        doc_id = create["id"]

        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "No Source Note",
                "content": "Updated",
                "agent": "test-agent",
                "id": doc_id,
                "access_scope": "task",
            },
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"
        assert "source_task" in result["message"]


class TestLithosWritePreExistingCompatibility:
    """Ensure pre-existing lithos_write callers work unchanged."""

    @pytest.mark.asyncio
    async def test_basic_create_unchanged(self, server: LithosServer) -> None:
        result = await _call_tool(
            server,
            "lithos_write",
            {"title": "Basic", "content": "Hello", "agent": "a"},
        )
        assert result["status"] == "created"
        assert "id" in result
        assert "path" in result
        assert "version" in result
        assert "warnings" in result

    @pytest.mark.asyncio
    async def test_basic_update_unchanged(self, server: LithosServer) -> None:
        create = await _call_tool(
            server,
            "lithos_write",
            {"title": "To Update", "content": "V1", "agent": "a"},
        )
        doc_id = create["id"]

        update = await _call_tool(
            server,
            "lithos_write",
            {"title": "To Update", "content": "V2", "agent": "a", "id": doc_id},
        )
        assert update["status"] == "updated"
        assert update["id"] == doc_id

    @pytest.mark.asyncio
    async def test_envelope_shape_unchanged(self, server: LithosServer) -> None:
        """Status envelope keys unchanged — no new top-level LCMA keys."""
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Envelope Test",
                "content": "Content",
                "agent": "a",
                "note_type": "concept",
            },
        )
        assert result["status"] == "created"
        # Envelope must only have these keys
        expected_keys = {"status", "id", "path", "version", "warnings"}
        assert set(result.keys()) == expected_keys
