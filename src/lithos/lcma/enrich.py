"""Background enrichment worker for LCMA.

Subscribes to the event bus, enqueues enrichment work into ``enrich_queue``,
and periodically drains the queue to apply node-level and task-level
enrichment asynchronously.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from lithos.events import (
    EDGE_UPSERTED,
    ENRICH_SUBSCRIBER_QUEUE_SIZE,
    FINDING_POSTED,
    NOTE_CREATED,
    NOTE_DELETED,
    NOTE_UPDATED,
    TASK_COMPLETED,
    LithosEvent,
)

if TYPE_CHECKING:
    from lithos.config import LcmaConfig
    from lithos.coordination import CoordinationService
    from lithos.events import EventBus
    from lithos.knowledge import KnowledgeManager
    from lithos.lcma.edges import EdgeStore
    from lithos.lcma.stats import StatsStore

logger = logging.getLogger(__name__)

_SUBSCRIBED_EVENT_TYPES = [
    NOTE_CREATED,
    NOTE_UPDATED,
    NOTE_DELETED,
    TASK_COMPLETED,
    FINDING_POSTED,
    EDGE_UPSERTED,
]


def _resolve_node_id(
    payload: dict[str, str | int | float | bool | None],
    knowledge: KnowledgeManager,
    event_type: str,
) -> str | None:
    """Resolve a knowledge node ID from an event payload.

    Returns ``None`` when the event does not map to a valid node.

    The normalization contract per event type:
    - ``note.created`` / ``note.updated``: use ``payload["id"]`` if present,
      otherwise resolve via ``knowledge.get_id_by_path(payload["path"])``.
    - ``note.deleted``: use ``payload["id"]`` only.  Do **not** check
      KnowledgeManager because the node has already been deleted.
    - ``finding.posted``: use ``payload["knowledge_id"]`` when present;
      validate against KnowledgeManager.  Skip when absent.
    - ``edge.upserted``: handled separately (two node IDs).
    - ``task.completed``: no node ID (task-level work).
    """
    if event_type in (NOTE_CREATED, NOTE_UPDATED):
        node_id = payload.get("id")
        if isinstance(node_id, str) and node_id:
            if knowledge.has_document(node_id):
                return node_id
            logger.debug("_resolve_node_id: node_id=%s not found in knowledge", node_id)
            return None
        path = payload.get("path")
        if isinstance(path, str) and path:
            resolved = knowledge.get_id_by_path(path)
            if resolved:
                return resolved
            logger.debug("_resolve_node_id: path=%s could not be resolved", path)
        return None

    if event_type == NOTE_DELETED:
        node_id = payload.get("id")
        if isinstance(node_id, str) and node_id:
            return node_id
        return None

    if event_type == FINDING_POSTED:
        kid = payload.get("knowledge_id")
        if not isinstance(kid, str) or not kid:
            return None
        if knowledge.has_document(kid):
            return kid
        logger.debug("_resolve_node_id: finding knowledge_id=%s not found in knowledge", kid)
        return None

    # task.completed and edge.upserted are handled by the caller
    return None


class EnrichWorker:
    """In-process background worker that consumes events and drains enrichment work."""

    def __init__(
        self,
        config: LcmaConfig,
        event_bus: EventBus,
        stats_store: StatsStore,
        edge_store: EdgeStore,
        knowledge: KnowledgeManager,
        coordination: CoordinationService,
    ) -> None:
        self._config = config
        self._event_bus = event_bus
        self._stats_store = stats_store
        self._edge_store = edge_store
        self._knowledge = knowledge
        self._coordination = coordination

        self._queue: asyncio.Queue[LithosEvent] | None = None
        self._consumer_task: asyncio.Task[None] | None = None
        self._drain_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Subscribe to events and start consumer + drain tasks."""
        self._queue = self._event_bus.subscribe(
            event_types=_SUBSCRIBED_EVENT_TYPES,
            maxsize=ENRICH_SUBSCRIBER_QUEUE_SIZE,
        )
        self._consumer_task = asyncio.create_task(self._consume_events(), name="enrich-consumer")
        self._drain_task = asyncio.create_task(self._drain_loop(), name="enrich-drain")
        logger.info(
            "EnrichWorker started (drain_interval=%dm, max_attempts=%d)",
            self._config.enrich_drain_interval_minutes,
            self._config.max_enrich_attempts,
        )

    async def stop(self) -> None:
        """Cancel tasks and unsubscribe from event bus."""
        for task in (self._consumer_task, self._drain_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        if self._queue is not None:
            self._event_bus.unsubscribe(self._queue)
            self._queue = None

        self._consumer_task = None
        self._drain_task = None
        logger.info("EnrichWorker stopped")

    # ------------------------------------------------------------------
    # Event consumer
    # ------------------------------------------------------------------

    async def _consume_events(self) -> None:
        """Read events from the subscription queue and enqueue work."""
        assert self._queue is not None
        try:
            while True:
                event = await self._queue.get()
                try:
                    await self._handle_event(event)
                except Exception:
                    logger.exception("EnrichWorker: error handling event %s", event.type)
        except asyncio.CancelledError:
            return

    async def _handle_event(self, event: LithosEvent) -> None:
        """Route a single event to enrich_queue."""
        if event.type == TASK_COMPLETED:
            task_id = event.payload.get("task_id")
            if isinstance(task_id, str) and task_id:
                await self._stats_store.enqueue(trigger_type=event.type, task_id=task_id)
            return

        if event.type == EDGE_UPSERTED:
            from_id = event.payload.get("from_id")
            to_id = event.payload.get("to_id")
            for nid in (from_id, to_id):
                if isinstance(nid, str) and nid:
                    if self._knowledge.has_document(nid):
                        await self._stats_store.enqueue(trigger_type=event.type, node_id=nid)
                    else:
                        logger.debug(
                            "EnrichWorker: edge.upserted node_id=%s not in knowledge, skipping",
                            nid,
                        )
            return

        # note.created, note.updated, note.deleted, finding.posted
        node_id = _resolve_node_id(event.payload, self._knowledge, event.type)
        if node_id is not None:
            await self._stats_store.enqueue(trigger_type=event.type, node_id=node_id)

    # ------------------------------------------------------------------
    # Drain loop
    # ------------------------------------------------------------------

    async def _drain_loop(self) -> None:
        """Periodically drain the enrich_queue."""
        interval = self._config.enrich_drain_interval_minutes * 60
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self.drain()
                except Exception:
                    logger.exception("EnrichWorker: drain cycle failed")
        except asyncio.CancelledError:
            return

    async def drain(self) -> None:
        """Process pending nodes and tasks from enrich_queue."""
        max_attempts = self._config.max_enrich_attempts

        # --- Node-level enrichment ---
        node_entries = await self._stats_store.drain_pending_nodes(max_attempts=max_attempts)
        for entry in node_entries:
            node_id = entry["node_id"]
            trigger_types = entry["trigger_types"]
            claimed_ids = entry["claimed_ids"]
            assert isinstance(node_id, str)
            assert isinstance(claimed_ids, list)
            try:
                await self._enrich_node(node_id, trigger_types)
            except Exception:
                logger.exception("EnrichWorker: node enrichment failed for %s, requeuing", node_id)
                await self._stats_store.requeue_failed(claimed_ids)

        # --- Task-level enrichment ---
        task_entries = await self._stats_store.drain_pending_tasks(max_attempts=max_attempts)
        for entry in task_entries:
            task_id = entry["task_id"]
            claimed_ids = entry["claimed_ids"]
            assert isinstance(task_id, str)
            assert isinstance(claimed_ids, list)
            try:
                await self._consolidate_task(task_id)
            except Exception:
                logger.exception(
                    "EnrichWorker: task consolidation failed for %s, requeuing", task_id
                )
                await self._stats_store.requeue_failed(claimed_ids)

    async def _enrich_node(self, node_id: str, trigger_types: object) -> None:
        """Apply node-level enrichment: salience decay and edge projection.

        Salience decay is applied when the node has been inactive longer than
        ``config.decay_inactive_days``.  Decay is convergent — running twice
        in the same day is safe because ``last_decay_applied_at`` is checked.

        Edge projection re-syncs ``derived_from`` edges for the node.
        """
        from lithos.lcma.edges import _project_node_provenance

        # --- Salience decay ---
        stats = await self._stats_store.get_node_stats(node_id)
        if stats is not None:
            await self._apply_decay(node_id, stats)

        # --- Edge projection ---
        await _project_node_provenance(self._edge_store, self._knowledge, node_id)

    async def _apply_decay(self, node_id: str, stats: dict[str, object]) -> None:
        """Apply salience decay to a single node.

        Convergent: skips if ``last_decay_applied_at`` is already today (UTC).
        """
        now = datetime.now(timezone.utc)

        # Check convergence — skip if already decayed today
        last_decay_raw = stats.get("last_decay_applied_at")
        if isinstance(last_decay_raw, str) and last_decay_raw:
            last_decay_dt = datetime.fromisoformat(last_decay_raw)
            if last_decay_dt.tzinfo is None:
                last_decay_dt = last_decay_dt.replace(tzinfo=timezone.utc)
            if last_decay_dt.date() == now.date():
                return

        # Determine days since last use
        last_used_raw = stats.get("last_used_at")
        if not isinstance(last_used_raw, str) or not last_used_raw:
            # Fallback to last_retrieved_at
            last_used_raw = stats.get("last_retrieved_at")
        if not isinstance(last_used_raw, str) or not last_used_raw:
            return  # No usage data — skip decay

        last_used_dt = datetime.fromisoformat(last_used_raw)
        if last_used_dt.tzinfo is None:
            last_used_dt = last_used_dt.replace(tzinfo=timezone.utc)

        days_since_last_use = (now - last_used_dt).days
        if days_since_last_use <= self._config.decay_inactive_days:
            return

        decay_amount = min(0.1, days_since_last_use * 0.005)
        await self._stats_store.update_salience(node_id, -decay_amount)
        await self._stats_store.update_last_decay_applied_at(node_id)
        logger.debug(
            "Applied decay to %s: days_inactive=%d, decay=%.3f",
            node_id,
            days_since_last_use,
            decay_amount,
        )

    async def _consolidate_task(self, task_id: str) -> None:
        """Consolidate working memory into long-term signals.

        Reads WM for the task, identifies frequently co-activated nodes
        (``activation_count >= 2``), reinforces ``related_to`` edges between
        pairs in the same namespace, and boosts salience for each frequent
        node.  Fully idempotent via per-target op tables plus the
        ``task_consolidation_log`` envelope.
        """
        # Task-level idempotency check
        if await self._stats_store.is_task_consolidated(task_id):
            logger.debug("Task %s already consolidated, skipping", task_id)
            return

        # Read working memory and filter to frequent nodes
        wm_entries = await self._stats_store.get_working_memory(task_id)
        frequent = [
            e
            for e in wm_entries
            if isinstance((ac := e.get("activation_count")), int) and ac >= 2
        ]

        if not frequent:
            await self._stats_store.mark_task_consolidated(task_id)
            return

        # Build node_id → namespace map (only nodes still in knowledge)
        node_ns: dict[str, str] = {}
        for entry in frequent:
            nid = str(entry["node_id"])
            cached = self._knowledge._meta_cache.get(nid)
            if cached is not None:
                node_ns[nid] = cached.namespace

        # --- Edge reinforcement between frequent node pairs ---
        node_ids = [str(e["node_id"]) for e in frequent if str(e["node_id"]) in node_ns]
        for i, a in enumerate(node_ids):
            for b in node_ids[i + 1 :]:
                ns_a = node_ns.get(a)
                ns_b = node_ns.get(b)
                if ns_a is None or ns_b is None or ns_a != ns_b:
                    continue

                # Canonical ordering
                from_id, to_id = (a, b) if a <= b else (b, a)
                ns = ns_a

                # Per-target idempotency: record-before-write (at-most-once
                # across stats.db ↔ edges.db boundary)
                if await self._stats_store.has_consolidation_edge_op(task_id, from_id, to_id):
                    continue
                await self._stats_store.record_consolidation_edge_op(task_id, from_id, to_id)

                # Upsert or adjust edge
                existing = await self._edge_store.list_edges(
                    from_id=from_id, to_id=to_id, edge_type="related_to", namespace=ns
                )
                if existing:
                    edge_id = str(existing[0]["edge_id"])
                    await self._edge_store.adjust_weight(edge_id, 0.03)
                else:
                    await self._edge_store.upsert(
                        from_id=from_id,
                        to_id=to_id,
                        edge_type="related_to",
                        weight=0.03,
                        namespace=ns,
                        provenance_type="consolidation",
                    )

        # --- Salience boost for each frequent node ---
        for entry in frequent:
            nid = str(entry["node_id"])
            if await self._stats_store.has_consolidation_salience_op(task_id, nid):
                continue
            # Atomic: salience update + op record in one stats.db transaction
            await self._stats_store.update_salience_and_record_consolidation(
                node_id=nid, delta=0.01, task_id=task_id
            )

        await self._stats_store.mark_task_consolidated(task_id)
        logger.debug("Consolidated task %s: %d frequent nodes", task_id, len(frequent))
