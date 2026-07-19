"""Internal event bus for Lithos.

Provides an in-memory pub/sub event bus that emits LithosEvent on all
write/delete/task/finding/agent-register success paths. Purely internal
infrastructure — no MCP tools, no SSE, no webhooks.

When disabled, emit() is a no-op (no fan-out, no buffer append).
emit() never raises — all exceptions are caught and logged.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from lithos.config import EventsConfig

from lithos.telemetry import get_tracer, lithos_metrics, register_event_bus_metrics

logger = logging.getLogger(__name__)

# --- Event type constants ---

NOTE_CREATED = "note.created"
NOTE_UPDATED = "note.updated"
NOTE_DELETED = "note.deleted"
NOTE_RENAMED = "note.renamed"

TASK_CREATED = "task.created"
TASK_UPDATED = "task.updated"
TASK_CLAIMED = "task.claimed"
TASK_RELEASED = "task.released"
TASK_COMPLETED = "task.completed"
TASK_CANCELLED = "task.cancelled"
TASK_REOPENED = "task.reopened"

FINDING_POSTED = "finding.posted"

EDGE_UPSERTED = "edge.upserted"

AGENT_REGISTERED = "agent.registered"

# --- Event origin markers ---
#
# An internal, non-caller-facing signal on ``LithosEvent.origin`` naming the
# subsystem that produced a write. Background workers use it to skip their own
# events (loop-break) WITHOUT overloading the caller-facing ``agent`` field.
# It is set only by trusted in-process callers (e.g. the enrich worker via the
# intake) and is never surfaced on the MCP tool schema or the SSE wire, so an
# external caller cannot forge it. Default ``""`` = ordinary external write.
EVENT_ORIGIN_ENRICH = "enrich"

BATCH_QUEUED = "batch.queued"
BATCH_APPLYING = "batch.applying"
BATCH_PROJECTING = "batch.projecting"
BATCH_COMPLETED = "batch.completed"
BATCH_FAILED = "batch.failed"

# Subscriber queue sizing for background workers.
# The default EventBus subscriber queue is 100, which silently drops events
# under load. lithos-enrich subscribes with a much larger queue to survive
# bursts during bulk writes or full-sweep cycles (see design doc §8.10).
ENRICH_SUBSCRIBER_QUEUE_SIZE = 10_000


@dataclass
class LithosEvent:
    """A typed event emitted by the Lithos event bus.

    ``origin`` is an internal subsystem marker (see the "Event origin markers"
    section above) — set only by trusted in-process callers, never surfaced on
    the MCP schema or SSE wire. It exists so background workers can drop their
    own events without keying on the spoofable ``agent`` field. It is appended
    **last** so adding it did not shift any existing positional field
    (``type``/``agent``/``payload``/``tags``/``id``/``timestamp``); all callers
    set it by keyword.
    """

    type: str
    agent: str = ""
    payload: dict[str, str | int | float | bool | None] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    origin: str = ""


def make_edge_upserted_event(
    *,
    agent: str,
    edge_id: str,
    from_id: str,
    to_id: str,
    edge_type: str,
    namespace: str,
    conflict_state: str | None = None,
    origin: str = "",
) -> LithosEvent:
    """Build the canonical :data:`EDGE_UPSERTED` event.

    Single source of the payload shape so every emitter
    (``CorpusIntake.assert_edge``, ``CognitiveMemory.conflict_resolve``) agrees
    on the same keys — previously the two hand-rolled dicts diverged (one had
    ``namespace``/``agent`` but no ``conflict_state``, the other the reverse).
    """
    return LithosEvent(
        type=EDGE_UPSERTED,
        agent=agent,
        origin=origin,
        payload={
            "edge_id": edge_id,
            "from_id": from_id,
            "to_id": to_id,
            "type": edge_type,
            "namespace": namespace,
            "conflict_state": conflict_state,
        },
    )


@dataclass
class _Subscriber:
    """Internal subscriber state."""

    queue: asyncio.Queue[LithosEvent]
    event_types: list[str] | None
    tag_filter: list[str] | None
    subscriber_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    drops: int = 0


class BufferedReplay(NamedTuple):
    """Result of :meth:`EventBus.get_buffered_since`.

    ``events`` are the buffered events after the reconnect id (in emission
    order). ``gapped`` is True when the id is not found in the ring, meaning the
    bus cannot prove continuity from it — the id was evicted, belongs to a
    previous server run (a fresh buffer after restart), or was never emitted —
    so the SSE stream should tell the client to resync rather than silently
    under-deliver. A previously-delivered id that is still buffered yields
    ``gapped=False``; a spurious resync for a fabricated id is harmless (it just
    re-fetches current state).
    """

    events: list[LithosEvent]
    gapped: bool


class EventBus:
    """In-memory event bus with filtered subscriptions and ring buffer history."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        if config is not None:
            self._enabled = config.enabled
            self._buffer_size = config.event_buffer_size
            self._queue_size = config.subscriber_queue_size
        else:
            self._enabled = True
            self._buffer_size = 500
            self._queue_size = 100

        self._buffer: deque[LithosEvent] = deque(maxlen=self._buffer_size)
        self._subscribers: list[_Subscriber] = []
        register_event_bus_metrics(self)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def emit(self, event: LithosEvent) -> None:
        """Emit an event to all matching subscribers.

        Non-blocking: if a subscriber queue is full, the event is dropped
        for that subscriber and a per-subscriber drop counter is incremented.

        Never raises — all exceptions are caught and logged.
        """
        if not self._enabled:
            return

        tracer = get_tracer()
        with tracer.start_as_current_span("lithos.event_bus.emit") as span:
            span.set_attribute("lithos.event.type", event.type)
            lithos_metrics.event_bus_ops.add(1, {"op": "emit", "event_type": event.type})
            logger.debug(
                "event_bus emit: event_type=%s event_id=%s agent=%s subscriber_count=%d",
                event.type,
                event.id,
                event.agent,
                len(self._subscribers),
                extra={
                    "event_type": event.type,
                    "event_id": event.id,
                    "agent": event.agent,
                    "subscriber_count": len(self._subscribers),
                },
            )

            try:
                self._buffer.append(event)
            except Exception:
                logger.exception("EventBus.emit: buffer append failed")
                return

            for sub in self._subscribers:
                try:
                    if not self._matches(event, sub):
                        continue
                    sub.queue.put_nowait(event)
                except asyncio.QueueFull:
                    sub.drops += 1
                    lithos_metrics.event_bus_ops.add(1, {"op": "drop", "event_type": event.type})
                    lithos_metrics.event_bus_subscriber_drops.add(
                        1, {"subscriber_id": sub.subscriber_id}
                    )
                    logger.warning(
                        "event_bus queue full: subscriber_id=%s event_type=%s total_drops=%d",
                        sub.subscriber_id,
                        event.type,
                        sub.drops,
                        extra={
                            "subscriber_id": sub.subscriber_id,
                            "event_type": event.type,
                            "total_drops": sub.drops,
                        },
                    )
                except Exception:
                    logger.exception("EventBus.emit: failed to deliver to subscriber")

    def subscribe(
        self,
        event_types: list[str] | None = None,
        tags: list[str] | None = None,
        maxsize: int | None = None,
    ) -> asyncio.Queue[LithosEvent]:
        """Subscribe to events, optionally filtered by type and/or tags.

        Returns a bounded asyncio.Queue that will receive matching events.

        Args:
            event_types: If provided, only events whose ``type`` is in this
                list will be delivered to this subscriber.
            tags: If provided, only events that carry at least one of these
                tags will be delivered.
            maxsize: Override the default subscriber queue size.  Pass
                ``ENRICH_SUBSCRIBER_QUEUE_SIZE`` here to absorb write bursts
                without dropping events (see design doc §8.10).
        """
        q_size = maxsize if maxsize is not None else self._queue_size
        queue: asyncio.Queue[LithosEvent] = asyncio.Queue(maxsize=q_size)
        sub = _Subscriber(queue=queue, event_types=event_types, tag_filter=tags)
        self._subscribers.append(sub)
        logger.debug(
            "event_bus subscribe: subscriber_id=%s event_types=%s queue_size=%d total_subscribers=%d",
            sub.subscriber_id,
            event_types,
            q_size,
            len(self._subscribers),
            extra={
                "subscriber_id": sub.subscriber_id,
                "event_types": event_types,
                "queue_size": q_size,
                "total_subscribers": len(self._subscribers),
            },
        )
        return queue

    def unsubscribe(self, queue: asyncio.Queue[LithosEvent]) -> None:
        """Remove a subscriber by its queue reference."""
        sub_id = self._get_subscriber_id(queue)
        self._subscribers = [s for s in self._subscribers if s.queue is not queue]
        logger.debug(
            "event_bus unsubscribe: subscriber_id=%s total_subscribers=%d",
            sub_id,
            len(self._subscribers),
            extra={"subscriber_id": sub_id, "total_subscribers": len(self._subscribers)},
        )

    def get_drop_count(self, queue: asyncio.Queue[LithosEvent]) -> int:
        """Get the drop counter for a subscriber queue."""
        for sub in self._subscribers:
            if sub.queue is queue:
                return sub.drops
        return 0

    def _get_subscriber_id(self, queue: asyncio.Queue[LithosEvent]) -> str | None:
        """Return the subscriber_id for a given queue, or None if not found."""
        for sub in self._subscribers:
            if sub.queue is queue:
                return sub.subscriber_id
        return None

    def get_buffer_utilisation(self) -> list[tuple[str, float]]:
        """Return per-subscriber buffer utilisation as (subscriber_id, ratio) pairs.

        The ratio is the current queue fill fraction in the range [0.0, 1.0].
        """
        result = []
        for sub in self._subscribers:
            maxsize = sub.queue.maxsize
            if maxsize > 0:
                result.append((sub.subscriber_id, sub.queue.qsize() / maxsize))
        return result

    def get_buffered_since(self, since_id: str) -> BufferedReplay:
        """Return buffered events after *since_id*, with a replay-gap signal.

        Used for SSE replay on reconnect. Events are returned in emission order,
        exclusive of *since_id*. Continuity is decided purely by whether
        *since_id* is still in the ring:

        - **Found** — the caller's position is known; return the events after it.
          A caught-up client (its id is the newest buffered event) gets
          ``([], gapped=False)``.
        - **Absent** — the bus cannot prove continuity: the id was evicted from
          the ring, belongs to a previous server run (a fresh buffer after a
          restart), or was never emitted. All three mean events may have been
          missed, so ``gapped=True`` and the SSE layer tells the client to
          resync. A spurious resync for a fabricated id is harmless (it just
          re-fetches current state); a silent under-delivery is not.

        Args:
            since_id: The event ID to replay from (exclusive).

        Returns:
            A :class:`BufferedReplay` — ``events`` after *since_id* and ``gapped``.
        """
        events = list(self._buffer)
        for i, event in enumerate(events):
            if event.id == since_id:
                return BufferedReplay(events[i + 1 :], gapped=False)
        return BufferedReplay([], gapped=True)

    @property
    def active_subscriber_count(self) -> int:
        """Return the number of active subscribers."""
        return len(self._subscribers)

    @staticmethod
    def _matches(event: LithosEvent, sub: _Subscriber) -> bool:
        """Check if an event matches a subscriber's filters."""
        if sub.event_types is not None and event.type not in sub.event_types:
            return False
        return not (sub.tag_filter is not None and not any(t in event.tags for t in sub.tag_filter))
