"""Tests for ``lithos.intake.CorpusIntake`` — the agent-driven Corpus mutation seam.

These tests pin contracts that are awkward to assert at the MCP handler tier:
event-after-view ordering, ensure-agent-before-mutation ordering, search-failure
no-event semantics, and the "event-emit failures don't undo writes" rule. PR 1
covers the delete path; the write path follows.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from lithos.config import LithosConfig
from lithos.coordination import CoordinationService
from lithos.events import NOTE_DELETED, EventBus, LithosEvent
from lithos.graph import KnowledgeGraph
from lithos.intake import CorpusIntake, DeleteOutcome, DeleteRequest
from lithos.knowledge import KnowledgeManager
from lithos.search import SearchEngine
from lithos.server import LithosServer

# ---------- Fixtures ----------


@pytest_asyncio.fixture
async def stub_intake(
    test_config: LithosConfig,
    knowledge_manager: KnowledgeManager,
    knowledge_graph: KnowledgeGraph,
) -> tuple[CorpusIntake, dict[str, Any]]:
    """A CorpusIntake wired with a real KnowledgeManager + graph but mocked
    SearchEngine, CoordinationService, and EventBus.

    Returns ``(intake, mocks)`` where ``mocks`` is a dict of the stubs so each
    test can configure side-effects and assert call patterns.
    """
    search = MagicMock(spec=SearchEngine)
    coordination = AsyncMock(spec=CoordinationService)
    event_bus = AsyncMock(spec=EventBus)

    intake = CorpusIntake(
        knowledge=knowledge_manager,
        search=search,
        graph=knowledge_graph,
        coordination=coordination,
        event_bus=event_bus,
    )
    return intake, {
        "search": search,
        "coordination": coordination,
        "event_bus": event_bus,
    }


# ---------- Unit tests ----------


@pytest.mark.asyncio
async def test_delete_returns_deleted_outcome_when_doc_exists(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
    knowledge_manager: KnowledgeManager,
) -> None:
    intake, mocks = stub_intake
    result = await knowledge_manager.create(
        title="To delete", content="bye", agent="agent-1", tags=["t"]
    )
    assert result.document is not None
    doc_id = result.document.id

    outcome = await intake.delete("agent-1", DeleteRequest(id=doc_id))

    assert isinstance(outcome, DeleteOutcome)
    assert outcome.status == "deleted"
    assert outcome.path  # non-empty relative path
    mocks["search"].remove.assert_called_once_with(doc_id)
    mocks["event_bus"].emit.assert_awaited_once()
    emitted_event = mocks["event_bus"].emit.await_args.args[0]
    assert emitted_event.type == NOTE_DELETED
    assert emitted_event.agent == "agent-1"
    assert emitted_event.payload["id"] == doc_id


@pytest.mark.asyncio
async def test_delete_returns_not_found_for_unknown_id(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
) -> None:
    intake, mocks = stub_intake

    outcome = await intake.delete("agent-1", DeleteRequest(id="does-not-exist"))

    assert outcome == DeleteOutcome(status="not_found")
    # Ensure-agent must still run for audit, but no view sync, no event.
    mocks["coordination"].ensure_agent_known.assert_awaited_once_with("agent-1")
    mocks["search"].remove.assert_not_called()
    mocks["event_bus"].emit.assert_not_called()


@pytest.mark.asyncio
async def test_delete_ensures_agent_before_corpus_mutation(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
    knowledge_manager: KnowledgeManager,
) -> None:
    intake, mocks = stub_intake
    result = await knowledge_manager.create(title="x", content="y", agent="agent-1")
    assert result.document is not None
    doc_id = result.document.id

    order: list[str] = []
    mocks["coordination"].ensure_agent_known.side_effect = lambda *_a, **_kw: (
        order.append("ensure_agent_known") or None
    )
    # Wrap knowledge.delete to record when it runs.
    original_delete = knowledge_manager.delete

    async def record_delete(target_id: str) -> tuple[bool, str]:
        order.append("knowledge.delete")
        return await original_delete(target_id)

    knowledge_manager.delete = record_delete  # type: ignore[method-assign]

    await intake.delete("agent-1", DeleteRequest(id=doc_id))

    assert order == ["ensure_agent_known", "knowledge.delete"]


@pytest.mark.asyncio
async def test_delete_emits_after_search_remove_returns(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
    knowledge_manager: KnowledgeManager,
) -> None:
    """Pins the event-after-indices invariant: ``event_bus.emit`` must not be
    awaited until ``search.remove`` has already returned."""
    intake, mocks = stub_intake
    result = await knowledge_manager.create(title="x", content="y", agent="agent-1")
    assert result.document is not None
    doc_id = result.document.id

    order: list[str] = []
    mocks["search"].remove.side_effect = lambda *_: order.append("search.remove")

    async def record_emit(event: LithosEvent) -> None:
        order.append(f"emit:{event.type}")

    mocks["event_bus"].emit.side_effect = record_emit

    await intake.delete("agent-1", DeleteRequest(id=doc_id))

    assert order == ["search.remove", f"emit:{NOTE_DELETED}"]


@pytest.mark.asyncio
async def test_delete_event_emit_failure_does_not_undo_corpus_delete(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
    knowledge_manager: KnowledgeManager,
) -> None:
    """A failed event delivery must never resurrect a successful Corpus
    delete — the corpus is the source of truth, the event is advisory."""
    intake, mocks = stub_intake
    result = await knowledge_manager.create(title="x", content="y", agent="agent-1")
    assert result.document is not None
    doc_id = result.document.id
    doc_path = result.document.path

    mocks["event_bus"].emit.side_effect = RuntimeError("bus down")

    outcome = await intake.delete("agent-1", DeleteRequest(id=doc_id))

    assert outcome.status == "deleted"
    # Doc is gone from the corpus despite the emit failure.
    assert knowledge_manager.get_id_by_path(doc_path) is None


@pytest.mark.asyncio
async def test_delete_search_failure_propagates_with_no_event(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
    knowledge_manager: KnowledgeManager,
) -> None:
    """``search.remove`` exceptions propagate — Drift is Reconcile's job
    (ADR-0001) — and no ``NOTE_DELETED`` event fires."""
    intake, mocks = stub_intake
    result = await knowledge_manager.create(title="x", content="y", agent="agent-1")
    assert result.document is not None
    doc_id = result.document.id
    doc_path = result.document.path

    mocks["search"].remove.side_effect = RuntimeError("search down")

    with pytest.raises(RuntimeError, match="search down"):
        await intake.delete("agent-1", DeleteRequest(id=doc_id))

    # Corpus already mutated — knowledge.delete ran before search.remove.
    assert knowledge_manager.get_id_by_path(doc_path) is None
    # No event should have been emitted because the operation aborted.
    mocks["event_bus"].emit.assert_not_called()


# ---------- Integration tests ----------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lithos_delete_handler_routes_through_intake(
    server: LithosServer,
) -> None:
    """End-to-end check that the ``lithos_delete`` MCP handler now goes
    through ``CorpusIntake`` — full pipeline observable via event bus."""
    create_tool = await server.mcp.get_tool("lithos_write")
    delete_tool = await server.mcp.get_tool("lithos_delete")

    create_result = await create_tool.fn(title="To delete", content="bye", agent="agent-1")
    doc_id = create_result["id"]

    queue = server.event_bus.subscribe(event_types=[NOTE_DELETED])
    try:
        delete_result = await delete_tool.fn(id=doc_id, agent="agent-1")
        assert delete_result == {"success": True}

        event = queue.get_nowait()
        assert event.type == NOTE_DELETED
        assert event.agent == "agent-1"
        assert event.payload["id"] == doc_id

        # Doc is gone from the search index too.
        assert server.knowledge.get_id_by_path(create_result["path"]) is None
    finally:
        server.event_bus.unsubscribe(queue)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lithos_delete_handler_returns_doc_not_found_envelope(
    server: LithosServer,
) -> None:
    delete_tool = await server.mcp.get_tool("lithos_delete")

    result = await delete_tool.fn(id="00000000-0000-0000-0000-000000000000", agent="agent-1")

    assert result == {
        "status": "error",
        "code": "doc_not_found",
        "message": "Document not found: 00000000-0000-0000-0000-000000000000",
    }
