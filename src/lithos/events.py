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
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lithos.config import EventsConfig

logger = logging.getLogger(__name__)

# --- Event type constants ---

NOTE_CREATED = "note.created"
NOTE_UPDATED = "note.updated"
NOTE_DELETED = "note.deleted"

TASK_CREATED = "task.created"
TASK_CLAIMED = "task.claimed"
TASK_RELEASED = "task.released"
TASK_COMPLETED = "task.completed"

FINDING_POSTED = "finding.posted"

AGENT_REGISTERED = "agent.registered"

BATCH_QUEUED = "batch.queued"
BATCH_APPLYING = "batch.applying"
BATCH_PROJECTING = "batch.projecting"
BATCH_COMPLETED = "batch.completed"
BATCH_FAILED = "batch.failed"


@dataclass
class LithosEvent:
    """A typed event emitted by the Lithos event bus."""

    type: str
    agent: str = ""
    payload: dict[str, str | int | float | bool | None] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class _Subscriber:
    """Internal subscriber state."""

    queue: asyncio.Queue[LithosEvent]
    event_types: list[str] | None
    tag_filter: list[str] | None
    drops: int = 0


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

        try:
            self._buffer.append(event)

            for sub in self._subscribers:
                if not self._matches(event, sub):
                    continue
                try:
                    sub.queue.put_nowait(event)
                except asyncio.QueueFull:
                    sub.drops += 1
        except Exception:
            logger.exception("EventBus.emit failed")

    def subscribe(
        self,
        event_types: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> asyncio.Queue[LithosEvent]:
        """Subscribe to events, optionally filtered by type and/or tags.

        Returns a bounded asyncio.Queue that will receive matching events.
        """
        queue: asyncio.Queue[LithosEvent] = asyncio.Queue(maxsize=self._queue_size)
        sub = _Subscriber(queue=queue, event_types=event_types, tag_filter=tags)
        self._subscribers.append(sub)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[LithosEvent]) -> None:
        """Remove a subscriber by its queue reference."""
        self._subscribers = [s for s in self._subscribers if s.queue is not queue]

    def get_drop_count(self, queue: asyncio.Queue[LithosEvent]) -> int:
        """Get the drop counter for a subscriber queue."""
        for sub in self._subscribers:
            if sub.queue is queue:
                return sub.drops
        return 0

    @staticmethod
    def _matches(event: LithosEvent, sub: _Subscriber) -> bool:
        """Check if an event matches a subscriber's filters."""
        if sub.event_types is not None and event.type not in sub.event_types:
            return False
        return not (sub.tag_filter is not None and not any(t in event.tags for t in sub.tag_filter))
