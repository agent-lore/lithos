"""Tests for the EnrichWorker background enrichment worker."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
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
from lithos.knowledge import KnowledgeManager
from lithos.lcma.edges import EdgeStore, _project_node_provenance
from lithos.lcma.enrich import EnrichWorker, _extract_entities_from_text, _resolve_node_id
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
    km.read = AsyncMock(side_effect=FileNotFoundError("mock doc not on disk"))
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


# ---------------------------------------------------------------------------
# Salience decay tests
# ---------------------------------------------------------------------------


class TestSalienceDecay:
    """Verify salience decay application over time."""

    async def test_decay_applied_after_inactive_days(
        self,
        worker: EnrichWorker,
        stats_store: StatsStore,
    ) -> None:
        """Salience decays after more than decay_inactive_days of inactivity."""
        node_id = "decay-test-1"
        # Create node_stats with last_used_at 14 days ago
        await stats_store.increment_node_stats(node_id=node_id)
        fourteen_days_ago = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            await db.execute(
                "UPDATE node_stats SET last_used_at = ? WHERE node_id = ?",
                (fourteen_days_ago, node_id),
            )
            await db.commit()

        stats_before = await stats_store.get_node_stats(node_id)
        assert stats_before is not None
        salience_before = stats_before["salience"]

        await worker._enrich_node(node_id, [NOTE_UPDATED])

        stats_after = await stats_store.get_node_stats(node_id)
        assert stats_after is not None
        assert stats_after["salience"] < salience_before
        # decay_amount = min(0.1, 14 * 0.005) = 0.07
        expected_salience = max(0.0, salience_before - 0.07)
        assert abs(stats_after["salience"] - expected_salience) < 0.001
        # last_decay_applied_at should be set
        assert stats_after["last_decay_applied_at"] is not None

    async def test_no_decay_within_inactive_days(
        self,
        worker: EnrichWorker,
        stats_store: StatsStore,
    ) -> None:
        """No decay when within decay_inactive_days threshold."""
        node_id = "decay-test-2"
        await stats_store.increment_node_stats(node_id=node_id)
        # Set last_used_at to 3 days ago (within default 7 day threshold)
        three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            await db.execute(
                "UPDATE node_stats SET last_used_at = ? WHERE node_id = ?",
                (three_days_ago, node_id),
            )
            await db.commit()

        stats_before = await stats_store.get_node_stats(node_id)
        assert stats_before is not None
        salience_before = stats_before["salience"]

        await worker._enrich_node(node_id, [NOTE_UPDATED])

        stats_after = await stats_store.get_node_stats(node_id)
        assert stats_after is not None
        assert stats_after["salience"] == salience_before

    async def test_decay_convergent_same_day(
        self,
        worker: EnrichWorker,
        stats_store: StatsStore,
    ) -> None:
        """Running decay twice on the same day is idempotent."""
        node_id = "decay-test-3"
        await stats_store.increment_node_stats(node_id=node_id)
        twenty_days_ago = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            await db.execute(
                "UPDATE node_stats SET last_used_at = ? WHERE node_id = ?",
                (twenty_days_ago, node_id),
            )
            await db.commit()

        # First decay
        await worker._enrich_node(node_id, [NOTE_UPDATED])
        stats_after_first = await stats_store.get_node_stats(node_id)
        assert stats_after_first is not None

        # Second decay — should be a no-op
        await worker._enrich_node(node_id, [NOTE_UPDATED])
        stats_after_second = await stats_store.get_node_stats(node_id)
        assert stats_after_second is not None
        assert stats_after_second["salience"] == stats_after_first["salience"]

    async def test_decay_uses_last_retrieved_at_fallback(
        self,
        worker: EnrichWorker,
        stats_store: StatsStore,
    ) -> None:
        """Falls back to last_retrieved_at when last_used_at is NULL."""
        node_id = "decay-test-4"
        await stats_store.increment_node_stats(node_id=node_id)
        # last_retrieved_at was set by increment_node_stats, set it to 10 days ago
        ten_days_ago = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            await db.execute(
                "UPDATE node_stats SET last_retrieved_at = ?, last_used_at = NULL "
                "WHERE node_id = ?",
                (ten_days_ago, node_id),
            )
            await db.commit()

        stats_before = await stats_store.get_node_stats(node_id)
        assert stats_before is not None

        await worker._enrich_node(node_id, [NOTE_UPDATED])

        stats_after = await stats_store.get_node_stats(node_id)
        assert stats_after is not None
        # decay_amount = min(0.1, 10 * 0.005) = 0.05
        assert stats_after["salience"] < stats_before["salience"]

    async def test_decay_capped_at_max(
        self,
        worker: EnrichWorker,
        stats_store: StatsStore,
    ) -> None:
        """Decay amount is capped at 0.1 regardless of days inactive."""
        node_id = "decay-test-5"
        await stats_store.increment_node_stats(node_id=node_id)
        long_ago = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            await db.execute(
                "UPDATE node_stats SET last_used_at = ? WHERE node_id = ?",
                (long_ago, node_id),
            )
            await db.commit()

        stats_before = await stats_store.get_node_stats(node_id)
        assert stats_before is not None
        salience_before = stats_before["salience"]

        await worker._enrich_node(node_id, [NOTE_UPDATED])

        stats_after = await stats_store.get_node_stats(node_id)
        assert stats_after is not None
        # decay_amount = min(0.1, 100 * 0.005) = min(0.1, 0.5) = 0.1
        expected = max(0.0, salience_before - 0.1)
        assert abs(stats_after["salience"] - expected) < 0.001


# ---------------------------------------------------------------------------
# Edge projection tests
# ---------------------------------------------------------------------------


class TestProjectNodeProvenance:
    """Verify _project_node_provenance for single-node edge sync."""

    async def test_deleted_node_removes_all_derived_from_edges(
        self,
        edge_store: EdgeStore,
        mock_knowledge: MagicMock,
    ) -> None:
        """Deleted node has all its derived_from edges removed."""
        # Create some derived_from edges where from_id is the deleted node
        await edge_store.upsert(
            from_id="deleted-node",
            to_id="source-1",
            edge_type="derived_from",
            weight=1.0,
            namespace="default",
        )
        await edge_store.upsert(
            from_id="deleted-node",
            to_id="source-2",
            edge_type="derived_from",
            weight=1.0,
            namespace="default",
        )

        # Node no longer exists
        mock_knowledge.has_document = MagicMock(return_value=False)

        result = await _project_node_provenance(edge_store, mock_knowledge, "deleted-node")
        assert result == {"created": 0, "removed": 2}

        # Verify edges are gone
        remaining = await edge_store.list_edges(from_id="deleted-node", edge_type="derived_from")
        assert len(remaining) == 0

    async def test_edge_projection_sync_for_existing_node(
        self,
        edge_store: EdgeStore,
        mock_knowledge: MagicMock,
    ) -> None:
        """Existing node gets derived_from edges synced from frontmatter."""
        node_id = "sync-node"

        # Pre-existing edge that should be removed (source-old is no longer in frontmatter)
        await edge_store.upsert(
            from_id=node_id,
            to_id="source-old",
            edge_type="derived_from",
            weight=1.0,
            namespace="default",
        )

        # Set up knowledge mock
        mock_knowledge.has_document = MagicMock(return_value=True)
        mock_knowledge.get_doc_sources = MagicMock(return_value=["source-new"])

        # Set up _meta_cache with a mock that has .namespace
        cached_meta = MagicMock()
        cached_meta.namespace = "default"
        mock_knowledge._meta_cache = {node_id: cached_meta}
        mock_knowledge.get_cached_meta = MagicMock(side_effect=mock_knowledge._meta_cache.get)

        result = await _project_node_provenance(edge_store, mock_knowledge, node_id)
        assert result["created"] == 1
        assert result["removed"] == 1

        # Verify: source-old edge gone, source-new edge exists
        edges = await edge_store.list_edges(from_id=node_id, edge_type="derived_from")
        assert len(edges) == 1
        assert edges[0]["to_id"] == "source-new"

    async def test_edge_projection_no_op_when_in_sync(
        self,
        edge_store: EdgeStore,
        mock_knowledge: MagicMock,
    ) -> None:
        """No changes when edges already match frontmatter."""
        node_id = "synced-node"

        await edge_store.upsert(
            from_id=node_id,
            to_id="source-1",
            edge_type="derived_from",
            weight=1.0,
            namespace="default",
        )

        mock_knowledge.has_document = MagicMock(return_value=True)
        mock_knowledge.get_doc_sources = MagicMock(return_value=["source-1"])

        cached_meta = MagicMock()
        cached_meta.namespace = "default"
        mock_knowledge._meta_cache = {node_id: cached_meta}
        mock_knowledge.get_cached_meta = MagicMock(side_effect=mock_knowledge._meta_cache.get)

        result = await _project_node_provenance(edge_store, mock_knowledge, node_id)
        assert result == {"created": 0, "removed": 0}


# ---------------------------------------------------------------------------
# Task consolidation tests
# ---------------------------------------------------------------------------


def _setup_meta_cache(
    mock_knowledge: MagicMock, node_ids: list[str], namespace: str = "default"
) -> None:
    """Helper: populate mock _meta_cache with namespace for given node_ids.

    Also wires ``get_cached_meta`` (the public accessor introduced in #171)
    to the same underlying dict so either access path resolves identically.
    """
    cache: dict[str, MagicMock] = {}
    for nid in node_ids:
        meta = MagicMock()
        meta.namespace = namespace
        cache[nid] = meta
    mock_knowledge._meta_cache = cache
    mock_knowledge.get_cached_meta = MagicMock(side_effect=cache.get)
    mock_knowledge.iter_cached_meta = MagicMock(side_effect=lambda: list(cache.items()))


class TestConsolidateTask:
    """Verify task-level consolidation logic."""

    async def test_consolidate_with_wm_entries(
        self,
        worker: EnrichWorker,
        stats_store: StatsStore,
        edge_store: EdgeStore,
        mock_knowledge: MagicMock,
    ) -> None:
        """Consolidation creates edges and boosts salience for frequent nodes."""
        task_id = "task-consolidate-1"
        nodes = ["node-a", "node-b", "node-c"]
        _setup_meta_cache(mock_knowledge, nodes)

        # Seed node_stats so salience reads work
        for nid in nodes:
            await stats_store.increment_node_stats(node_id=nid)

        # Insert WM entries: node-a x3, node-b x2, node-c x1 (not frequent)
        for nid, count in [("node-a", 3), ("node-b", 2), ("node-c", 1)]:
            for _ in range(count):
                await stats_store.upsert_working_memory(
                    task_id=task_id, node_id=nid, receipt_id="rcpt_test"
                )

        salience_before_a = (await stats_store.get_node_stats("node-a"))["salience"]
        salience_before_b = (await stats_store.get_node_stats("node-b"))["salience"]
        salience_before_c = (await stats_store.get_node_stats("node-c"))["salience"]

        await worker._consolidate_task(task_id)

        # Edge between node-a and node-b should exist (both frequent, same ns)
        edges = await edge_store.list_edges(
            from_id="node-a", to_id="node-b", edge_type="related_to", namespace="default"
        )
        assert len(edges) == 1
        assert abs(edges[0]["weight"] - 0.03) < 0.001

        # Salience boosted for frequent nodes only
        salience_after_a = (await stats_store.get_node_stats("node-a"))["salience"]
        salience_after_b = (await stats_store.get_node_stats("node-b"))["salience"]
        salience_after_c = (await stats_store.get_node_stats("node-c"))["salience"]

        assert abs(salience_after_a - (salience_before_a + 0.01)) < 0.001
        assert abs(salience_after_b - (salience_before_b + 0.01)) < 0.001
        assert salience_after_c == salience_before_c  # not frequent

        # Task marked as consolidated
        assert await stats_store.is_task_consolidated(task_id)

    async def test_consolidate_no_wm_entries(
        self,
        worker: EnrichWorker,
        stats_store: StatsStore,
        mock_knowledge: MagicMock,
    ) -> None:
        """Consolidation with no WM entries is a no-op but marks task done."""
        task_id = "task-empty"
        mock_knowledge._meta_cache = {}
        mock_knowledge.get_cached_meta = MagicMock(side_effect=mock_knowledge._meta_cache.get)
        mock_knowledge.iter_cached_meta = MagicMock(side_effect=lambda: [])

        await worker._consolidate_task(task_id)

        assert await stats_store.is_task_consolidated(task_id)

    async def test_consolidate_already_consolidated(
        self,
        worker: EnrichWorker,
        stats_store: StatsStore,
        edge_store: EdgeStore,
        mock_knowledge: MagicMock,
    ) -> None:
        """Re-consolidating an already-consolidated task is a no-op."""
        task_id = "task-idempotent"
        nodes = ["node-x", "node-y"]
        _setup_meta_cache(mock_knowledge, nodes)

        for nid in nodes:
            await stats_store.increment_node_stats(node_id=nid)
        for nid in nodes:
            for _ in range(2):
                await stats_store.upsert_working_memory(
                    task_id=task_id, node_id=nid, receipt_id="rcpt_test"
                )

        # First consolidation
        await worker._consolidate_task(task_id)
        salience_after_first = (await stats_store.get_node_stats("node-x"))["salience"]
        edges_after_first = await edge_store.list_edges(
            from_id="node-x", to_id="node-y", edge_type="related_to"
        )

        # Second consolidation — should be a no-op
        await worker._consolidate_task(task_id)
        salience_after_second = (await stats_store.get_node_stats("node-x"))["salience"]
        edges_after_second = await edge_store.list_edges(
            from_id="node-x", to_id="node-y", edge_type="related_to"
        )

        assert salience_after_second == salience_after_first
        assert edges_after_second[0]["weight"] == edges_after_first[0]["weight"]

    async def test_partial_consolidation_replay(
        self,
        worker: EnrichWorker,
        stats_store: StatsStore,
        edge_store: EdgeStore,
        mock_knowledge: MagicMock,
    ) -> None:
        """After partial consolidation, replay skips applied ops and applies remaining."""
        task_id = "task-partial"
        nodes = ["node-p", "node-q", "node-r"]
        _setup_meta_cache(mock_knowledge, nodes)

        for nid in nodes:
            await stats_store.increment_node_stats(node_id=nid)
        for nid in nodes:
            for _ in range(2):
                await stats_store.upsert_working_memory(
                    task_id=task_id, node_id=nid, receipt_id="rcpt_test"
                )

        # Simulate partial consolidation: edge (node-p, node-q) already applied
        await stats_store.record_consolidation_edge_op(task_id, "node-p", "node-q")
        await edge_store.upsert(
            from_id="node-p",
            to_id="node-q",
            edge_type="related_to",
            weight=0.03,
            namespace="default",
            provenance_type="consolidation",
        )
        # Simulate partial consolidation: salience for node-p already applied
        await stats_store.update_salience_and_record_consolidation(
            node_id="node-p", delta=0.01, task_id=task_id
        )

        salience_p_before = (await stats_store.get_node_stats("node-p"))["salience"]
        salience_q_before = (await stats_store.get_node_stats("node-q"))["salience"]

        # Run consolidation — should skip already-applied ops
        await worker._consolidate_task(task_id)

        # node-p ↔ node-q edge should NOT be double-adjusted
        pq_edges = await edge_store.list_edges(
            from_id="node-p", to_id="node-q", edge_type="related_to"
        )
        assert len(pq_edges) == 1
        assert abs(pq_edges[0]["weight"] - 0.03) < 0.001  # unchanged

        # node-p ↔ node-r edge should be created (not yet applied)
        pr_edges = await edge_store.list_edges(
            from_id="node-p", to_id="node-r", edge_type="related_to"
        )
        assert len(pr_edges) == 1
        assert abs(pr_edges[0]["weight"] - 0.03) < 0.001

        # node-q ↔ node-r edge should be created
        qr_edges = await edge_store.list_edges(
            from_id="node-q", to_id="node-r", edge_type="related_to"
        )
        assert len(qr_edges) == 1

        # node-p salience should NOT be double-boosted
        salience_p_after = (await stats_store.get_node_stats("node-p"))["salience"]
        assert abs(salience_p_after - salience_p_before) < 0.001

        # node-q salience SHOULD be boosted (not previously applied)
        salience_q_after = (await stats_store.get_node_stats("node-q"))["salience"]
        assert abs(salience_q_after - (salience_q_before + 0.01)) < 0.001

        assert await stats_store.is_task_consolidated(task_id)


# ---------------------------------------------------------------------------
# Entity extraction tests
# ---------------------------------------------------------------------------


class TestExtractEntitiesFromText:
    """Verify rule-based entity extraction from text."""

    def test_wiki_links_extracted(self) -> None:
        text = "This references [[Knowledge Graph]] and [[NetworkX]]."
        entities = _extract_entities_from_text(text)
        assert "Knowledge Graph" in entities
        assert "NetworkX" in entities

    def test_backtick_terms_extracted(self) -> None:
        text = "The `EnrichWorker` processes events from the `EventBus`."
        entities = _extract_entities_from_text(text)
        assert "EnrichWorker" in entities
        assert "EventBus" in entities

    def test_capitalized_phrases_extracted(self) -> None:
        text = "The Knowledge Manager handles all document operations."
        entities = _extract_entities_from_text(text)
        assert "Knowledge Manager" in entities

    def test_proper_nouns_extracted(self) -> None:
        text = "Lithos uses Tantivy for full-text search and ChromaDB for semantic."
        entities = _extract_entities_from_text(text)
        assert "Lithos" in entities
        assert "Tantivy" in entities
        assert "ChromaDB" in entities

    def test_common_words_excluded(self) -> None:
        text = "The system should handle these cases. However this is fine."
        entities = _extract_entities_from_text(text)
        assert "The" not in entities
        assert "However" not in entities

    def test_wiki_link_with_display_text(self) -> None:
        text = "See [[target-doc|display name]] for details."
        entities = _extract_entities_from_text(text)
        assert "target-doc" in entities

    def test_deduplicated_and_sorted(self) -> None:
        text = "[[Alpha]] appears twice: [[Alpha]] and `Beta`."
        entities = _extract_entities_from_text(text)
        assert entities == sorted(set(entities))

    def test_empty_text(self) -> None:
        assert _extract_entities_from_text("") == []


class TestExtractEntities:
    """Verify _extract_entities integration with KnowledgeManager."""

    async def test_extract_entities_updates_frontmatter(
        self,
        test_config: LithosConfig,
        lcma_config: LcmaConfig,
        event_bus: EventBus,
        stats_store: StatsStore,
        edge_store: EdgeStore,
        mock_coordination: AsyncMock,
    ) -> None:
        """Entity extraction writes entities to frontmatter when absent."""
        km = KnowledgeManager(test_config)

        # Create a note with known entities in the content
        result = await km.create(
            title="Test Entity Note",
            content="Lithos uses [[NetworkX]] and `ChromaDB` for Knowledge Graph operations.",
            agent="test-agent",
        )
        assert result.document is not None
        doc_id = result.document.id

        worker = EnrichWorker(
            config=lcma_config,
            event_bus=event_bus,
            stats_store=stats_store,
            edge_store=edge_store,
            knowledge=km,
            coordination=mock_coordination,
        )

        await worker._extract_entities(doc_id)

        # Read back and verify entities were written
        doc, _ = await km.read(id=doc_id)
        assert len(doc.metadata.entities) > 0
        assert "NetworkX" in doc.metadata.entities
        assert "ChromaDB" in doc.metadata.entities

    async def test_agent_written_entities_not_overwritten(
        self,
        test_config: LithosConfig,
        lcma_config: LcmaConfig,
        event_bus: EventBus,
        stats_store: StatsStore,
        edge_store: EdgeStore,
        mock_coordination: AsyncMock,
    ) -> None:
        """Agent-written entities are preserved and not overwritten."""
        km = KnowledgeManager(test_config)

        # Create a note
        result = await km.create(
            title="Agent Entity Note",
            content="Lithos uses [[NetworkX]] for graph operations.",
            agent="test-agent",
        )
        assert result.document is not None
        doc_id = result.document.id

        # Agent writes custom entities
        agent_entities = ["CustomEntity", "AgentDefined"]
        await km.update(id=doc_id, agent="test-agent", entities=agent_entities)

        worker = EnrichWorker(
            config=lcma_config,
            event_bus=event_bus,
            stats_store=stats_store,
            edge_store=edge_store,
            knowledge=km,
            coordination=mock_coordination,
        )

        await worker._extract_entities(doc_id)

        # Read back — entities should still be the agent-written ones
        doc, _ = await km.read(id=doc_id)
        assert doc.metadata.entities == agent_entities

    async def test_enrich_node_extracts_entities_only_on_create_or_update(
        self,
        test_config: LithosConfig,
        lcma_config: LcmaConfig,
        event_bus: EventBus,
        stats_store: StatsStore,
        edge_store: EdgeStore,
        mock_coordination: AsyncMock,
    ) -> None:
        """_enrich_node only extracts entities when trigger_types includes note.created/updated."""
        km = KnowledgeManager(test_config)

        result = await km.create(
            title="Trigger Gating Test",
            content="Lithos uses [[NetworkX]] for graph operations.",
            agent="test-agent",
        )
        assert result.document is not None
        doc_id = result.document.id

        worker = EnrichWorker(
            config=lcma_config,
            event_bus=event_bus,
            stats_store=stats_store,
            edge_store=edge_store,
            knowledge=km,
            coordination=mock_coordination,
        )

        # Call with edge.upserted trigger — should NOT extract entities
        await worker._enrich_node(doc_id, [EDGE_UPSERTED])
        doc, _ = await km.read(id=doc_id)
        assert doc.metadata.entities == []

        # Call with note.created trigger — SHOULD extract entities
        await worker._enrich_node(doc_id, [NOTE_CREATED])
        doc, _ = await km.read(id=doc_id)
        assert len(doc.metadata.entities) > 0
        assert "NetworkX" in doc.metadata.entities

    async def test_entity_extraction_ignores_fenced_code_blocks(
        self,
        test_config: LithosConfig,
        lcma_config: LcmaConfig,
        event_bus: EventBus,
        stats_store: StatsStore,
        edge_store: EdgeStore,
        mock_coordination: AsyncMock,
    ) -> None:
        """Entity extraction should not extract identifiers from fenced code blocks."""
        km = KnowledgeManager(test_config)

        content = (
            "This note discusses Python.\n\n"
            "```python\n"
            "class MyInternalClass:\n"
            "    pass\n"
            "```\n\n"
            "The [[NetworkX]] library is used."
        )
        result = await km.create(
            title="Code Block Test",
            content=content,
            agent="test-agent",
        )
        assert result.document is not None
        doc_id = result.document.id

        worker = EnrichWorker(
            config=lcma_config,
            event_bus=event_bus,
            stats_store=stats_store,
            edge_store=edge_store,
            knowledge=km,
            coordination=mock_coordination,
        )

        await worker._extract_entities(doc_id)
        doc, _ = await km.read(id=doc_id)
        # NetworkX should be extracted (wiki-link outside code fence)
        assert "NetworkX" in doc.metadata.entities
        # MyInternalClass should NOT be extracted (inside code fence)
        assert "MyInternalClass" not in doc.metadata.entities


# ---------------------------------------------------------------------------
# Full sweep tests
# ---------------------------------------------------------------------------


class TestFullSweep:
    """Verify full sweep: decay, WM eviction, provenance reconciliation."""

    async def test_full_sweep_decays_nodes_and_evicts_wm(
        self,
        test_config: LithosConfig,
        lcma_config: LcmaConfig,
        event_bus: EventBus,
        stats_store: StatsStore,
        edge_store: EdgeStore,
    ) -> None:
        """Full sweep applies decay to inactive nodes and evicts stale WM."""
        from lithos.coordination import CoordinationService

        coord = CoordinationService(test_config)
        await coord.initialize()
        km = KnowledgeManager(test_config)

        worker = EnrichWorker(
            config=lcma_config,
            event_bus=event_bus,
            stats_store=stats_store,
            edge_store=edge_store,
            knowledge=km,
            coordination=coord,
        )

        # Create node_stats with last_used_at 14 days ago (will decay)
        node_id = "sweep-decay-1"
        await stats_store.increment_node_stats(node_id=node_id)
        fourteen_days_ago = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            await db.execute(
                "UPDATE node_stats SET last_used_at = ? WHERE node_id = ?",
                (fourteen_days_ago, node_id),
            )
            await db.commit()

        salience_before = (await stats_store.get_node_stats(node_id))["salience"]

        # Create a completed task with WM entries
        await coord.create_task(title="test-task", agent="test-agent")
        tasks = await coord.list_tasks(status="open")
        task_id = tasks[0]["id"]
        await coord.complete_task(task_id=task_id, agent="test-agent")

        # Add WM entries for the completed task
        await stats_store.upsert_working_memory(
            task_id=task_id, node_id=node_id, receipt_id="rcpt_test"
        )

        await worker.full_sweep()

        # Verify decay was applied
        salience_after = (await stats_store.get_node_stats(node_id))["salience"]
        assert salience_after < salience_before

        # Verify WM was evicted (completed task entries should be gone)
        wm = await stats_store.get_working_memory(task_id)
        assert len(wm) == 0

    async def test_full_sweep_repairs_stale_derived_from_edge(
        self,
        test_config: LithosConfig,
        lcma_config: LcmaConfig,
        event_bus: EventBus,
        stats_store: StatsStore,
        edge_store: EdgeStore,
    ) -> None:
        """Full sweep reconciles provenance edges, removing stale ones."""
        from lithos.coordination import CoordinationService

        coord = CoordinationService(test_config)
        await coord.initialize()
        km = KnowledgeManager(test_config)

        worker = EnrichWorker(
            config=lcma_config,
            event_bus=event_bus,
            stats_store=stats_store,
            edge_store=edge_store,
            knowledge=km,
            coordination=coord,
        )

        # Create two notes: source-note and derived-note
        source_result = await km.create(
            title="Source Note",
            content="This is a source.",
            agent="test-agent",
        )
        assert source_result.document is not None
        source_id = source_result.document.id

        derived_result = await km.create(
            title="Derived Note",
            content="This is derived.",
            agent="test-agent",
            derived_from_ids=[source_id],
        )
        assert derived_result.document is not None
        derived_id = derived_result.document.id

        # Add a stale derived_from edge that doesn't match frontmatter
        await edge_store.upsert(
            from_id=derived_id,
            to_id="nonexistent-source",
            edge_type="derived_from",
            weight=1.0,
            namespace="default",
        )

        # Verify the stale edge exists
        stale_edges = await edge_store.list_edges(
            from_id=derived_id, to_id="nonexistent-source", edge_type="derived_from"
        )
        assert len(stale_edges) == 1

        await worker.full_sweep()

        # Stale edge should be removed
        stale_edges_after = await edge_store.list_edges(
            from_id=derived_id, to_id="nonexistent-source", edge_type="derived_from"
        )
        assert len(stale_edges_after) == 0

        # Valid derived_from edge should still exist (or be created)
        valid_edges = await edge_store.list_edges(
            from_id=derived_id, to_id=source_id, edge_type="derived_from"
        )
        assert len(valid_edges) == 1


# ---------------------------------------------------------------------------
# Fix-regression tests
# ---------------------------------------------------------------------------


class TestAttemptsColumnMigration:
    """Verify legacy stats.db without attempts column is migrated."""

    async def test_legacy_db_without_attempts_column(
        self,
        test_config: LithosConfig,
    ) -> None:
        """Opening a stats.db that lacks the attempts column adds it via migration."""
        import aiosqlite

        db_path = test_config.storage.stats_db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # Create a legacy schema without the attempts column
        legacy_schema = """
        CREATE TABLE IF NOT EXISTS node_stats (
            node_id TEXT PRIMARY KEY,
            salience REAL NOT NULL DEFAULT 0.5,
            retrieval_count INTEGER NOT NULL DEFAULT 0,
            last_retrieved_at TIMESTAMP,
            last_used_at TIMESTAMP,
            ignored_count INTEGER NOT NULL DEFAULT 0,
            misleading_count INTEGER NOT NULL DEFAULT 0,
            decay_rate REAL NOT NULL DEFAULT 0.0,
            spaced_rep_strength REAL NOT NULL DEFAULT 0.0,
            cited_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS enrich_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trigger_type TEXT NOT NULL,
            node_id TEXT,
            task_id TEXT,
            triggered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            processed_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS working_memory (
            task_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            activation_count INTEGER NOT NULL DEFAULT 0,
            first_seen_at TIMESTAMP,
            last_seen_at TIMESTAMP,
            last_receipt_id TEXT,
            PRIMARY KEY (task_id, node_id)
        );
        CREATE TABLE IF NOT EXISTS receipts (
            id TEXT PRIMARY KEY,
            ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            query TEXT NOT NULL,
            "limit" INTEGER NOT NULL,
            namespace_filter TEXT,
            scouts_fired TEXT NOT NULL,
            candidates_considered INTEGER NOT NULL DEFAULT 0,
            final_nodes TEXT NOT NULL,
            conflicts_surfaced TEXT NOT NULL,
            surface_conflicts INTEGER NOT NULL DEFAULT 0,
            temperature REAL NOT NULL,
            terrace_reached INTEGER NOT NULL DEFAULT 0,
            agent_id TEXT,
            task_id TEXT
        );
        CREATE TABLE IF NOT EXISTS coactivation (
            node_id_a TEXT NOT NULL,
            node_id_b TEXT NOT NULL,
            namespace TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            last_at TIMESTAMP,
            PRIMARY KEY (node_id_a, node_id_b, namespace)
        );
        """
        async with aiosqlite.connect(db_path) as db:
            await db.executescript(legacy_schema)
            await db.commit()

        # Verify attempts column does NOT exist yet
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("PRAGMA table_info(enrich_queue)")
            columns = {row[1] for row in await cursor.fetchall()}
            assert "attempts" not in columns

        # Open StatsStore — migration should add the column
        store = StatsStore(test_config)
        store._opened = False  # force re-open
        await store.open()

        # Now drain and requeue should work without errors
        await store.enqueue(trigger_type="note.created", node_id="test-node")
        entries = await store.drain_pending_nodes(max_attempts=3)
        assert len(entries) == 1
        claimed_ids = entries[0]["claimed_ids"]
        assert isinstance(claimed_ids, list)
        count = await store.requeue_failed(claimed_ids)
        assert count == len(claimed_ids)


class TestRetryCapWarning:
    """Verify WARNING logged when retry cap is exhausted."""

    async def test_warning_logged_on_exhausted_retry(
        self,
        worker: EnrichWorker,
        stats_store: StatsStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When a node hits max_enrich_attempts, a WARNING is logged."""
        import logging

        await stats_store.enqueue(trigger_type=NOTE_CREATED, node_id="doc-exhaust")

        async def failing_enrich(node_id: str, trigger_types: object) -> None:
            raise RuntimeError("simulated failure")

        worker._enrich_node = failing_enrich  # type: ignore[assignment]

        # Drain 3 times to exhaust the 3 attempts
        with caplog.at_level(logging.WARNING, logger="lithos.lcma.enrich"):
            for _ in range(3):
                await worker.drain()

        assert any("exhausted retry cap" in r.message for r in caplog.records)
        assert any(r.levelno == logging.WARNING for r in caplog.records if "exhausted" in r.message)


class TestProjectNodeProvenanceUpsertStaleEdge:
    """Verify _project_node_provenance upserts existing edges with stale metadata."""

    async def test_stale_edge_metadata_resynced(
        self,
        edge_store: EdgeStore,
        mock_knowledge: MagicMock,
    ) -> None:
        """A pre-existing derived_from edge with non-canonical metadata is resynced."""
        node_id = "upsert-test-node"

        # Create a derived_from edge with stale metadata (wrong weight and provenance_type)
        await edge_store.upsert(
            from_id=node_id,
            to_id="source-1",
            edge_type="derived_from",
            weight=0.5,  # stale: canonical is 1.0
            namespace="default",
            provenance_type="manual",  # stale: canonical is "frontmatter"
        )

        # Verify stale values
        edges_before = await edge_store.list_edges(
            from_id=node_id, to_id="source-1", edge_type="derived_from"
        )
        assert len(edges_before) == 1
        assert edges_before[0]["weight"] == 0.5
        assert edges_before[0]["provenance_type"] == "manual"

        # Set up knowledge mock
        mock_knowledge.has_document = MagicMock(return_value=True)
        mock_knowledge.get_doc_sources = MagicMock(return_value=["source-1"])
        cached_meta = MagicMock()
        cached_meta.namespace = "default"
        mock_knowledge._meta_cache = {node_id: cached_meta}
        mock_knowledge.get_cached_meta = MagicMock(side_effect=mock_knowledge._meta_cache.get)

        result = await _project_node_provenance(edge_store, mock_knowledge, node_id)
        # No new edges created, no orphans removed — but existing one was upserted
        assert result == {"created": 0, "removed": 0}

        # Verify the edge now has canonical metadata
        edges_after = await edge_store.list_edges(
            from_id=node_id, to_id="source-1", edge_type="derived_from"
        )
        assert len(edges_after) == 1
        assert edges_after[0]["weight"] == 1.0
        assert edges_after[0]["provenance_type"] == "frontmatter"
