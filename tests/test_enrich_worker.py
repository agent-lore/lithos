"""Tests for the EnrichWorker background enrichment worker."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from lithos.config import LcmaConfig, LithosConfig
from lithos.events import (
    EDGE_UPSERTED,
    FINDING_POSTED,
    NOTE_CREATED,
    NOTE_DELETED,
    NOTE_UPDATED,
    TASK_COMPLETED,
    EventBus,
    LithosEvent,
)
from lithos.lcma.edges import EdgeStore
from lithos.lcma.enrich import EnrichWorker, _resolve_node_id
from lithos.lcma.stats import StatsStore


@pytest_asyncio.fixture
async def stats_store(test_config: LithosConfig) -> StatsStore:
    store = StatsStore(test_config)
    await store.open()
    return store


@pytest_asyncio.fixture
async def edge_store(test_config: LithosConfig) -> EdgeStore:
    store = EdgeStore(test_config)
    await store.open()
    return store


@pytest.fixture
def event_bus(test_config: LithosConfig) -> EventBus:
    return EventBus(test_config.events)


@pytest.fixture
def mock_knowledge() -> MagicMock:
    km = MagicMock()
    km.has_document = MagicMock(return_value=True)
    km.get_id_by_path = MagicMock(return_value=None)
    return km


@pytest.fixture
def mock_coordination() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def lcma_config() -> LcmaConfig:
    return LcmaConfig(enrich_drain_interval_minutes=1, max_enrich_attempts=3)


@pytest_asyncio.fixture
async def worker(
    lcma_config: LcmaConfig,
    event_bus: EventBus,
    stats_store: StatsStore,
    edge_store: EdgeStore,
    mock_knowledge: MagicMock,
    mock_coordination: AsyncMock,
) -> EnrichWorker:
    return EnrichWorker(
        config=lcma_config,
        event_bus=event_bus,
        stats_store=stats_store,
        edge_store=edge_store,
        knowledge=mock_knowledge,
        coordination=mock_coordination,
    )


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Start / stop lifecycle."""

    async def test_start_stop(self, worker: EnrichWorker) -> None:
        """Worker starts consumer + drain tasks and stops cleanly."""
        await worker.start()
        assert worker._consumer_task is not None
        assert worker._drain_task is not None
        assert not worker._consumer_task.done()
        assert not worker._drain_task.done()

        await worker.stop()
        assert worker._consumer_task is None
        assert worker._drain_task is None
        assert worker._queue is None

    async def test_stop_is_idempotent(self, worker: EnrichWorker) -> None:
        """Stopping an un-started worker is a no-op."""
        await worker.stop()
        assert worker._consumer_task is None


# ---------------------------------------------------------------------------
# Event consumer tests
# ---------------------------------------------------------------------------


class TestEventConsumer:
    """Events flow through to enrich_queue."""

    async def test_note_created_event_enqueued(
        self,
        worker: EnrichWorker,
        event_bus: EventBus,
        stats_store: StatsStore,
    ) -> None:
        """note.created event is enqueued as a node-level row."""
        await worker.start()
        try:
            await event_bus.emit(
                LithosEvent(
                    type=NOTE_CREATED,
                    payload={"id": "doc-1", "path": "notes/test.md"},
                )
            )
            # Give consumer time to process
            await asyncio.sleep(0.1)

            entries = await stats_store.drain_pending_nodes()
            assert len(entries) == 1
            assert entries[0]["node_id"] == "doc-1"
            assert NOTE_CREATED in entries[0]["trigger_types"]
        finally:
            await worker.stop()

    async def test_note_updated_event_enqueued(
        self,
        worker: EnrichWorker,
        event_bus: EventBus,
        stats_store: StatsStore,
    ) -> None:
        """note.updated event is enqueued."""
        await worker.start()
        try:
            await event_bus.emit(
                LithosEvent(
                    type=NOTE_UPDATED,
                    payload={"id": "doc-2", "path": "notes/test2.md"},
                )
            )
            await asyncio.sleep(0.1)

            entries = await stats_store.drain_pending_nodes()
            assert len(entries) == 1
            assert entries[0]["node_id"] == "doc-2"
        finally:
            await worker.stop()

    async def test_task_completed_event_enqueued(
        self,
        worker: EnrichWorker,
        event_bus: EventBus,
        stats_store: StatsStore,
    ) -> None:
        """task.completed event is enqueued as task-level row."""
        await worker.start()
        try:
            await event_bus.emit(
                LithosEvent(
                    type=TASK_COMPLETED,
                    payload={"task_id": "task-1"},
                )
            )
            await asyncio.sleep(0.1)

            entries = await stats_store.drain_pending_tasks()
            assert len(entries) == 1
            assert entries[0]["task_id"] == "task-1"
        finally:
            await worker.stop()

    async def test_edge_upserted_enqueues_both_nodes(
        self,
        worker: EnrichWorker,
        event_bus: EventBus,
        stats_store: StatsStore,
    ) -> None:
        """edge.upserted enqueues both from_id and to_id."""
        await worker.start()
        try:
            await event_bus.emit(
                LithosEvent(
                    type=EDGE_UPSERTED,
                    payload={"from_id": "node-a", "to_id": "node-b"},
                )
            )
            await asyncio.sleep(0.1)

            entries = await stats_store.drain_pending_nodes()
            node_ids = {e["node_id"] for e in entries}
            assert node_ids == {"node-a", "node-b"}
        finally:
            await worker.stop()

    async def test_edge_upserted_nonexistent_node_dropped(
        self,
        worker: EnrichWorker,
        event_bus: EventBus,
        stats_store: StatsStore,
        mock_knowledge: MagicMock,
    ) -> None:
        """edge.upserted with nonexistent from_id or to_id is dropped."""
        mock_knowledge.has_document = MagicMock(side_effect=lambda nid: nid == "node-a")
        await worker.start()
        try:
            await event_bus.emit(
                LithosEvent(
                    type=EDGE_UPSERTED,
                    payload={"from_id": "node-a", "to_id": "node-nonexistent"},
                )
            )
            await asyncio.sleep(0.1)

            entries = await stats_store.drain_pending_nodes()
            assert len(entries) == 1
            assert entries[0]["node_id"] == "node-a"
        finally:
            await worker.stop()

    async def test_finding_posted_nonexistent_knowledge_id_dropped(
        self,
        worker: EnrichWorker,
        event_bus: EventBus,
        stats_store: StatsStore,
        mock_knowledge: MagicMock,
    ) -> None:
        """finding.posted with nonexistent knowledge_id is dropped."""
        mock_knowledge.has_document = MagicMock(return_value=False)
        await worker.start()
        try:
            await event_bus.emit(
                LithosEvent(
                    type=FINDING_POSTED,
                    payload={
                        "finding_id": "f-1",
                        "task_id": "t-1",
                        "agent": "test",
                        "knowledge_id": "nonexistent",
                    },
                )
            )
            await asyncio.sleep(0.1)

            entries = await stats_store.drain_pending_nodes()
            assert len(entries) == 0
        finally:
            await worker.stop()

    async def test_note_deleted_enqueued_even_if_absent(
        self,
        worker: EnrichWorker,
        event_bus: EventBus,
        stats_store: StatsStore,
        mock_knowledge: MagicMock,
    ) -> None:
        """note.deleted with valid id is enqueued even if node no longer exists."""
        # has_document returns False (node already deleted), but _resolve_node_id
        # for note.deleted does not check existence.
        mock_knowledge.has_document = MagicMock(return_value=False)
        await worker.start()
        try:
            await event_bus.emit(
                LithosEvent(
                    type=NOTE_DELETED,
                    payload={"id": "deleted-doc", "path": "notes/gone.md"},
                )
            )
            await asyncio.sleep(0.1)

            entries = await stats_store.drain_pending_nodes()
            assert len(entries) == 1
            assert entries[0]["node_id"] == "deleted-doc"
        finally:
            await worker.stop()

    async def test_finding_posted_no_knowledge_id_skipped(
        self,
        worker: EnrichWorker,
        event_bus: EventBus,
        stats_store: StatsStore,
    ) -> None:
        """finding.posted without knowledge_id is skipped."""
        await worker.start()
        try:
            await event_bus.emit(
                LithosEvent(
                    type=FINDING_POSTED,
                    payload={"finding_id": "f-1", "task_id": "t-1", "agent": "test"},
                )
            )
            await asyncio.sleep(0.1)

            entries = await stats_store.drain_pending_nodes()
            assert len(entries) == 0
        finally:
            await worker.stop()


# ---------------------------------------------------------------------------
# Drain loop tests
# ---------------------------------------------------------------------------


class TestDrainLoop:
    """Drain loop processes and marks items."""

    async def test_drain_processes_pending_nodes(
        self,
        worker: EnrichWorker,
        stats_store: StatsStore,
    ) -> None:
        """drain() claims and processes pending node entries."""
        await stats_store.enqueue(trigger_type=NOTE_CREATED, node_id="doc-1")
        await stats_store.enqueue(trigger_type=NOTE_UPDATED, node_id="doc-1")

        await worker.drain()

        # After drain, no pending entries remain
        remaining = await stats_store.drain_pending_nodes()
        assert len(remaining) == 0

    async def test_drain_processes_pending_tasks(
        self,
        worker: EnrichWorker,
        stats_store: StatsStore,
    ) -> None:
        """drain() claims and processes pending task entries."""
        await stats_store.enqueue(trigger_type=TASK_COMPLETED, task_id="task-1")

        await worker.drain()

        remaining = await stats_store.drain_pending_tasks()
        assert len(remaining) == 0

    async def test_drain_requeues_on_failure(
        self,
        worker: EnrichWorker,
        stats_store: StatsStore,
    ) -> None:
        """When enrichment fails, drain requeues the items with incremented attempts."""
        await stats_store.enqueue(trigger_type=NOTE_CREATED, node_id="doc-fail")

        # Make _enrich_node raise
        async def failing_enrich(node_id: str, trigger_types: object) -> None:
            raise RuntimeError("simulated failure")

        worker._enrich_node = failing_enrich  # type: ignore[assignment]

        await worker.drain()

        # The row should be requeued (processed_at=NULL, attempts=1)
        remaining = await stats_store.drain_pending_nodes()
        assert len(remaining) == 1
        assert remaining[0]["node_id"] == "doc-fail"

    async def test_drain_respects_max_attempts(
        self,
        worker: EnrichWorker,
        stats_store: StatsStore,
    ) -> None:
        """Items exceeding max_attempts are not claimed."""
        await stats_store.enqueue(trigger_type=NOTE_CREATED, node_id="doc-retry")

        # Simulate 3 failures (max_enrich_attempts=3)
        async def failing_enrich(node_id: str, trigger_types: object) -> None:
            raise RuntimeError("simulated failure")

        worker._enrich_node = failing_enrich  # type: ignore[assignment]

        # Drain 3 times, each time the item gets requeued with incremented attempts
        for _ in range(3):
            await worker.drain()

        # After 3 failures, attempts == 3, which is not < max_enrich_attempts (3)
        # So drain should find no pending items
        remaining = await stats_store.drain_pending_nodes(max_attempts=3)
        assert len(remaining) == 0


# ---------------------------------------------------------------------------
# _resolve_node_id tests
# ---------------------------------------------------------------------------


class TestResolveNodeId:
    """Unit tests for _resolve_node_id helper."""

    def test_note_created_with_id(self, mock_knowledge: MagicMock) -> None:
        result = _resolve_node_id(
            {"id": "doc-1", "path": "notes/a.md"}, mock_knowledge, NOTE_CREATED
        )
        assert result == "doc-1"

    def test_note_created_with_path_fallback(self, mock_knowledge: MagicMock) -> None:
        mock_knowledge.has_document = MagicMock(return_value=False)
        mock_knowledge.get_id_by_path = MagicMock(return_value="resolved-id")
        result = _resolve_node_id({"path": "notes/a.md"}, mock_knowledge, NOTE_CREATED)
        assert result == "resolved-id"

    def test_note_deleted_uses_id_only(self, mock_knowledge: MagicMock) -> None:
        mock_knowledge.has_document = MagicMock(return_value=False)
        result = _resolve_node_id(
            {"id": "deleted-doc", "path": "notes/gone.md"}, mock_knowledge, NOTE_DELETED
        )
        assert result == "deleted-doc"

    def test_note_deleted_without_id_returns_none(self, mock_knowledge: MagicMock) -> None:
        result = _resolve_node_id({"path": "notes/gone.md"}, mock_knowledge, NOTE_DELETED)
        assert result is None

    def test_finding_posted_with_valid_knowledge_id(self, mock_knowledge: MagicMock) -> None:
        result = _resolve_node_id({"knowledge_id": "doc-1"}, mock_knowledge, FINDING_POSTED)
        assert result == "doc-1"

    def test_finding_posted_without_knowledge_id(self, mock_knowledge: MagicMock) -> None:
        result = _resolve_node_id({}, mock_knowledge, FINDING_POSTED)
        assert result is None

    def test_task_completed_returns_none(self, mock_knowledge: MagicMock) -> None:
        result = _resolve_node_id({"task_id": "task-1"}, mock_knowledge, TASK_COMPLETED)
        assert result is None
