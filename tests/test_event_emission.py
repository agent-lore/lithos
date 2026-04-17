"""Integration tests for event emission from server tool handlers."""

import json
from typing import Any

import pytest

from lithos.events import (
    AGENT_REGISTERED,
    FINDING_POSTED,
    NOTE_CREATED,
    NOTE_DELETED,
    NOTE_UPDATED,
    TASK_CLAIMED,
    TASK_COMPLETED,
    TASK_CREATED,
    TASK_RELEASED,
    LithosEvent,
)
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


class TestNoteEventEmission:
    """Test event emission from note (knowledge) tool handlers."""

    @pytest.mark.asyncio
    async def test_lithos_write_create_emits_note_created(self, server: LithosServer) -> None:
        queue = server.event_bus.subscribe(event_types=[NOTE_CREATED])
        result = await _call_tool(
            server,
            "lithos_write",
            {"title": "Test Note", "content": "Hello world", "agent": "test-agent", "tags": ["t1"]},
        )
        assert result["status"] == "created"

        event = queue.get_nowait()
        assert event.type == NOTE_CREATED
        assert event.agent == "test-agent"
        assert event.payload["id"] == result["id"]
        assert event.payload["title"] == "Test Note"
        assert event.payload["path"] == result["path"]
        assert event.tags == ["t1"]
        server.event_bus.unsubscribe(queue)

    @pytest.mark.asyncio
    async def test_lithos_write_update_emits_note_updated(self, server: LithosServer) -> None:
        create_result = await _call_tool(
            server,
            "lithos_write",
            {"title": "Original", "content": "Content", "agent": "test-agent"},
        )
        doc_id = create_result["id"]

        queue = server.event_bus.subscribe(event_types=[NOTE_UPDATED])
        update_result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Updated",
                "content": "New content",
                "agent": "test-agent",
                "id": doc_id,
            },
        )
        assert update_result["status"] == "updated"

        event = queue.get_nowait()
        assert event.type == NOTE_UPDATED
        assert event.agent == "test-agent"
        assert event.payload["id"] == doc_id
        assert event.payload["title"] == "Updated"
        server.event_bus.unsubscribe(queue)

    @pytest.mark.asyncio
    async def test_lithos_write_duplicate_emits_no_event(self, server: LithosServer) -> None:
        await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Unique Note",
                "content": "Content",
                "agent": "test-agent",
                "source_url": "https://example.com/unique",
            },
        )

        queue = server.event_bus.subscribe()
        dup_result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Unique Note Dup",
                "content": "Content dup",
                "agent": "test-agent",
                "source_url": "https://example.com/unique",
            },
        )
        assert dup_result["status"] == "duplicate"
        assert queue.empty()
        server.event_bus.unsubscribe(queue)

    @pytest.mark.asyncio
    async def test_lithos_delete_emits_note_deleted(self, server: LithosServer) -> None:
        create_result = await _call_tool(
            server,
            "lithos_write",
            {"title": "To Delete", "content": "Content", "agent": "test-agent"},
        )
        doc_id = create_result["id"]

        queue = server.event_bus.subscribe(event_types=[NOTE_DELETED])
        delete_result = await _call_tool(
            server,
            "lithos_delete",
            {"id": doc_id, "agent": "test-agent"},
        )
        assert delete_result["success"] is True

        event = queue.get_nowait()
        assert event.type == NOTE_DELETED
        assert event.agent == "test-agent"
        assert event.payload["id"] == doc_id
        assert event.payload["path"], "delete event must include the document path"
        server.event_bus.unsubscribe(queue)


class TestTaskEventEmission:
    """Test event emission from task coordination tool handlers."""

    @pytest.mark.asyncio
    async def test_lithos_task_create_emits_task_created(self, server: LithosServer) -> None:
        queue = server.event_bus.subscribe(event_types=[TASK_CREATED])
        result = await _call_tool(
            server,
            "lithos_task_create",
            {"title": "Test Task", "agent": "test-agent"},
        )
        task_id = result["task_id"]

        event = queue.get_nowait()
        assert event.type == TASK_CREATED
        assert event.agent == "test-agent"
        assert event.payload["task_id"] == task_id
        assert event.payload["title"] == "Test Task"
        server.event_bus.unsubscribe(queue)

    @pytest.mark.asyncio
    async def test_lithos_task_claim_emits_task_claimed(self, server: LithosServer) -> None:
        result = await _call_tool(
            server,
            "lithos_task_create",
            {"title": "Claimable Task", "agent": "test-agent"},
        )
        task_id = result["task_id"]

        queue = server.event_bus.subscribe(event_types=[TASK_CLAIMED])
        claim_result = await _call_tool(
            server,
            "lithos_task_claim",
            {"task_id": task_id, "aspect": "research", "agent": "test-agent"},
        )
        assert claim_result["success"] is True

        event = queue.get_nowait()
        assert event.type == TASK_CLAIMED
        assert event.agent == "test-agent"
        assert event.payload["task_id"] == task_id
        assert event.payload["aspect"] == "research"
        server.event_bus.unsubscribe(queue)

    @pytest.mark.asyncio
    async def test_lithos_task_release_emits_task_released(self, server: LithosServer) -> None:
        result = await _call_tool(
            server,
            "lithos_task_create",
            {"title": "Releasable Task", "agent": "test-agent"},
        )
        task_id = result["task_id"]
        await _call_tool(
            server,
            "lithos_task_claim",
            {"task_id": task_id, "aspect": "research", "agent": "test-agent"},
        )

        queue = server.event_bus.subscribe(event_types=[TASK_RELEASED])
        release_result = await _call_tool(
            server,
            "lithos_task_release",
            {"task_id": task_id, "aspect": "research", "agent": "test-agent"},
        )
        assert release_result["success"] is True

        event = queue.get_nowait()
        assert event.type == TASK_RELEASED
        assert event.agent == "test-agent"
        assert event.payload["task_id"] == task_id
        assert event.payload["aspect"] == "research"
        server.event_bus.unsubscribe(queue)

    @pytest.mark.asyncio
    async def test_lithos_task_complete_emits_task_completed(self, server: LithosServer) -> None:
        result = await _call_tool(
            server,
            "lithos_task_create",
            {"title": "Completable Task", "agent": "test-agent"},
        )
        task_id = result["task_id"]

        queue = server.event_bus.subscribe(event_types=[TASK_COMPLETED])
        complete_result = await _call_tool(
            server,
            "lithos_task_complete",
            {"task_id": task_id, "agent": "test-agent"},
        )
        assert complete_result["success"] is True

        event = queue.get_nowait()
        assert event.type == TASK_COMPLETED
        assert event.agent == "test-agent"
        assert event.payload["task_id"] == task_id
        # outcome is surfaced in the event payload; None when not supplied.
        assert event.payload["outcome"] is None
        server.event_bus.unsubscribe(queue)

    @pytest.mark.asyncio
    async def test_lithos_task_complete_accepts_outcome(self, server: LithosServer) -> None:
        """Regression test for #178: ``outcome`` must be an accepted parameter
        and must flow into the persisted task + the task.completed event."""
        result = await _call_tool(
            server,
            "lithos_task_create",
            {"title": "Task with Outcome", "agent": "test-agent"},
        )
        task_id = result["task_id"]

        queue = server.event_bus.subscribe(event_types=[TASK_COMPLETED])
        outcome_text = "Reviewed LCMA scout architecture and filed 3 follow-ups."
        complete_result = await _call_tool(
            server,
            "lithos_task_complete",
            {"task_id": task_id, "agent": "test-agent", "outcome": outcome_text},
        )
        assert complete_result["success"] is True

        # Event payload carries the outcome verbatim.
        event = queue.get_nowait()
        assert event.type == TASK_COMPLETED
        assert event.payload["outcome"] == outcome_text
        server.event_bus.unsubscribe(queue)

        # Task row persists the outcome for later consolidation / audit.
        task = await server.coordination.get_task(task_id)
        assert task is not None
        assert task.outcome == outcome_text
        assert task.completed_at is not None


class TestFindingEventEmission:
    """Test event emission from finding tool handlers."""

    @pytest.mark.asyncio
    async def test_lithos_finding_post_emits_finding_posted(self, server: LithosServer) -> None:
        task_result = await _call_tool(
            server,
            "lithos_task_create",
            {"title": "Finding Task", "agent": "test-agent"},
        )
        task_id = task_result["task_id"]

        queue = server.event_bus.subscribe(event_types=[FINDING_POSTED])
        finding_result = await _call_tool(
            server,
            "lithos_finding_post",
            {"task_id": task_id, "agent": "test-agent", "summary": "Found something"},
        )
        finding_id = finding_result["finding_id"]

        event = queue.get_nowait()
        assert event.type == FINDING_POSTED
        assert event.agent == "test-agent"
        assert event.payload["finding_id"] == finding_id
        assert event.payload["task_id"] == task_id
        assert event.payload["agent"] == "test-agent"
        server.event_bus.unsubscribe(queue)


class TestAgentEventEmission:
    """Test event emission from agent tool handlers."""

    @pytest.mark.asyncio
    async def test_lithos_agent_register_emits_agent_registered(self, server: LithosServer) -> None:
        queue = server.event_bus.subscribe(event_types=[AGENT_REGISTERED])
        result = await _call_tool(
            server,
            "lithos_agent_register",
            {"id": "event-test-agent", "name": "Event Test Agent"},
        )
        assert result["success"] is True

        event = queue.get_nowait()
        assert event.type == AGENT_REGISTERED
        assert event.agent == "event-test-agent"
        assert event.payload["agent_id"] == "event-test-agent"
        assert event.payload["name"] == "Event Test Agent"
        server.event_bus.unsubscribe(queue)


class TestEventEmissionFailureIsolation:
    """Test that event emission failures do not affect tool handler operations."""

    @pytest.mark.asyncio
    async def test_failed_emit_does_not_break_write(self, server: LithosServer) -> None:
        original_emit = server.event_bus.emit

        async def broken_emit(event: LithosEvent) -> None:
            raise RuntimeError("Event bus is broken!")

        server.event_bus.emit = broken_emit  # type: ignore[assignment]
        try:
            result = await _call_tool(
                server,
                "lithos_write",
                {"title": "Resilient Note", "content": "Still works", "agent": "test-agent"},
            )
            assert result["status"] == "created"
            assert "id" in result
        finally:
            server.event_bus.emit = original_emit  # type: ignore[assignment]

    @pytest.mark.asyncio
    async def test_subscriber_backpressure_does_not_fail_operation(
        self, server: LithosServer
    ) -> None:
        queue = server.event_bus.subscribe(event_types=[NOTE_CREATED])

        # Fill the subscriber queue to capacity
        for i in range(server.event_bus._queue_size):
            await server.event_bus.emit(
                LithosEvent(type=NOTE_CREATED, agent="filler", payload={"i": str(i)})
            )

        # Queue is now full; next emit should drop for this subscriber but not fail
        result = await _call_tool(
            server,
            "lithos_write",
            {"title": "Backpressure Test", "content": "Content", "agent": "test-agent"},
        )
        assert result["status"] == "created"

        # The subscriber's drop count should have been incremented
        assert server.event_bus.get_drop_count(queue) >= 1
        server.event_bus.unsubscribe(queue)

    @pytest.mark.asyncio
    async def test_failed_delete_path_emits_no_event(self, server: LithosServer) -> None:
        queue = server.event_bus.subscribe(event_types=[NOTE_DELETED])

        # Try to delete a non-existent document
        result = await _call_tool(
            server,
            "lithos_delete",
            {"id": "nonexistent-uuid", "agent": "test-agent"},
        )
        assert result.get("status") == "error" or result.get("success") is False
        assert queue.empty()
        server.event_bus.unsubscribe(queue)
