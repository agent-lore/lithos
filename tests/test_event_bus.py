"""Unit tests for the internal event bus."""

import pytest

from lithos.events import (
    AGENT_REGISTERED,
    NOTE_CREATED,
    NOTE_DELETED,
    NOTE_UPDATED,
    TASK_CLAIMED,
    TASK_COMPLETED,
    TASK_CREATED,
    EventBus,
    LithosEvent,
)


@pytest.fixture
def bus() -> EventBus:
    """Create an EventBus with small buffer/queue for testing."""
    return EventBus()


class TestLithosEvent:
    """Tests for LithosEvent dataclass."""

    def test_auto_id(self) -> None:
        e1 = LithosEvent(type=NOTE_CREATED)
        e2 = LithosEvent(type=NOTE_CREATED)
        assert e1.id != e2.id
        assert len(e1.id) == 36  # UUID format

    def test_stable_unique_id(self) -> None:
        """Each event gets a unique, stable id."""
        ids = {LithosEvent(type=NOTE_CREATED).id for _ in range(100)}
        assert len(ids) == 100

    def test_defaults(self) -> None:
        e = LithosEvent(type=NOTE_CREATED)
        assert e.type == NOTE_CREATED
        assert e.agent == ""
        assert e.payload == {}
        assert e.tags == []
        assert e.timestamp is not None

    def test_custom_fields(self) -> None:
        e = LithosEvent(
            type=NOTE_UPDATED,
            agent="test-agent",
            payload={"id": "abc", "title": "Test"},
            tags=["python"],
        )
        assert e.agent == "test-agent"
        assert e.payload["title"] == "Test"
        assert e.tags == ["python"]


class TestEventBusEmitReceive:
    """Tests for basic emit/receive."""

    async def test_emit_receive(self, bus: EventBus) -> None:
        queue = bus.subscribe()
        event = LithosEvent(type=NOTE_CREATED, payload={"id": "123"})
        await bus.emit(event)
        received = queue.get_nowait()
        assert received.id == event.id
        assert received.type == NOTE_CREATED

    async def test_multiple_subscribers(self, bus: EventBus) -> None:
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        event = LithosEvent(type=NOTE_CREATED)
        await bus.emit(event)
        assert q1.get_nowait().id == event.id
        assert q2.get_nowait().id == event.id

    async def test_best_effort_in_order_delivery(self, bus: EventBus) -> None:
        """Sequential same-loop emits are delivered in order."""
        queue = bus.subscribe()
        events = [LithosEvent(type=NOTE_CREATED, payload={"seq": str(i)}) for i in range(10)]
        for e in events:
            await bus.emit(e)
        for i in range(10):
            received = queue.get_nowait()
            assert received.payload["seq"] == str(i)


class TestSubscribeWithTypeFilter:
    """Tests for type-filtered subscriptions."""

    async def test_type_filter_matches(self, bus: EventBus) -> None:
        queue = bus.subscribe(event_types=[NOTE_CREATED])
        await bus.emit(LithosEvent(type=NOTE_CREATED))
        await bus.emit(LithosEvent(type=NOTE_DELETED))
        assert queue.qsize() == 1
        assert queue.get_nowait().type == NOTE_CREATED

    async def test_type_filter_multiple_types(self, bus: EventBus) -> None:
        queue = bus.subscribe(event_types=[TASK_CREATED, TASK_COMPLETED])
        await bus.emit(LithosEvent(type=TASK_CREATED))
        await bus.emit(LithosEvent(type=TASK_CLAIMED))
        await bus.emit(LithosEvent(type=TASK_COMPLETED))
        assert queue.qsize() == 2

    async def test_no_filter_receives_all(self, bus: EventBus) -> None:
        queue = bus.subscribe()
        await bus.emit(LithosEvent(type=NOTE_CREATED))
        await bus.emit(LithosEvent(type=TASK_CREATED))
        await bus.emit(LithosEvent(type=AGENT_REGISTERED))
        assert queue.qsize() == 3


class TestSubscribeWithTagFilter:
    """Tests for tag-filtered subscriptions."""

    async def test_tag_filter_matches(self, bus: EventBus) -> None:
        queue = bus.subscribe(tags=["python"])
        await bus.emit(LithosEvent(type=NOTE_CREATED, tags=["python", "testing"]))
        await bus.emit(LithosEvent(type=NOTE_CREATED, tags=["docker"]))
        assert queue.qsize() == 1

    async def test_tag_filter_any_match(self, bus: EventBus) -> None:
        """Tag filter matches if ANY subscribed tag matches ANY event tag."""
        queue = bus.subscribe(tags=["python", "docker"])
        await bus.emit(LithosEvent(type=NOTE_CREATED, tags=["docker"]))
        assert queue.qsize() == 1

    async def test_tag_filter_no_tags_on_event(self, bus: EventBus) -> None:
        """Events with no tags don't match a tag filter."""
        queue = bus.subscribe(tags=["python"])
        await bus.emit(LithosEvent(type=NOTE_CREATED, tags=[]))
        assert queue.qsize() == 0

    async def test_combined_type_and_tag_filter(self, bus: EventBus) -> None:
        queue = bus.subscribe(event_types=[NOTE_CREATED], tags=["python"])
        await bus.emit(LithosEvent(type=NOTE_CREATED, tags=["python"]))
        await bus.emit(LithosEvent(type=NOTE_UPDATED, tags=["python"]))
        await bus.emit(LithosEvent(type=NOTE_CREATED, tags=["docker"]))
        assert queue.qsize() == 1


class TestRingBuffer:
    """Tests for the ring buffer."""

    async def test_ring_buffer_wraps_at_capacity(self) -> None:
        from lithos.config import EventsConfig

        config = EventsConfig(event_buffer_size=5)
        bus = EventBus(config)
        for i in range(10):
            await bus.emit(LithosEvent(type=NOTE_CREATED, payload={"seq": str(i)}))
        # Buffer should only contain last 5 events
        assert len(bus._buffer) == 5
        assert bus._buffer[0].payload["seq"] == "5"
        assert bus._buffer[-1].payload["seq"] == "9"


class TestDropOnFull:
    """Tests for backpressure handling."""

    async def test_drop_on_full_increments_counter(self) -> None:
        from lithos.config import EventsConfig

        config = EventsConfig(subscriber_queue_size=2)
        bus = EventBus(config)
        queue = bus.subscribe()
        # Emit 5 events — queue holds 2, so 3 should be dropped
        for _ in range(5):
            await bus.emit(LithosEvent(type=NOTE_CREATED))
        assert queue.qsize() == 2
        assert bus.get_drop_count(queue) == 3

    async def test_emit_never_raises_on_full(self) -> None:
        from lithos.config import EventsConfig

        config = EventsConfig(subscriber_queue_size=1)
        bus = EventBus(config)
        bus.subscribe()
        # Should not raise even when queue overflows
        for _ in range(100):
            await bus.emit(LithosEvent(type=NOTE_CREATED))


class TestDisabled:
    """Tests for disabled event bus."""

    async def test_noop_when_disabled(self) -> None:
        from lithos.config import EventsConfig

        config = EventsConfig(enabled=False)
        bus = EventBus(config)
        queue = bus.subscribe()
        await bus.emit(LithosEvent(type=NOTE_CREATED))
        assert queue.qsize() == 0
        assert len(bus._buffer) == 0


class TestUnsubscribe:
    """Tests for unsubscribe cleanup."""

    async def test_unsubscribe_removes_subscriber(self, bus: EventBus) -> None:
        queue = bus.subscribe()
        bus.unsubscribe(queue)
        await bus.emit(LithosEvent(type=NOTE_CREATED))
        assert queue.qsize() == 0

    async def test_unsubscribe_does_not_affect_others(self, bus: EventBus) -> None:
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        bus.unsubscribe(q1)
        await bus.emit(LithosEvent(type=NOTE_CREATED))
        assert q1.qsize() == 0
        assert q2.qsize() == 1

    async def test_drop_count_zero_after_unsubscribe(self, bus: EventBus) -> None:
        queue = bus.subscribe()
        bus.unsubscribe(queue)
        assert bus.get_drop_count(queue) == 0


class TestSubscriberIdAndMetrics:
    """Tests for subscriber_id and the new OTEL metric instruments."""

    def test_subscribe_assigns_subscriber_id(self) -> None:
        """Each subscribe() call assigns a unique subscriber_id."""
        bus = EventBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        id1 = bus.get_subscriber_id(q1)
        id2 = bus.get_subscriber_id(q2)
        assert id1 is not None
        assert id2 is not None
        assert id1 != id2
        # UUID v4 format (36 chars)
        assert len(id1) == 36
        assert len(id2) == 36

    def test_get_subscriber_id_unknown_queue_returns_none(self) -> None:
        """get_subscriber_id returns None for an unregistered queue."""
        import asyncio

        bus = EventBus()
        orphan: asyncio.Queue = asyncio.Queue(maxsize=10)
        assert bus.get_subscriber_id(orphan) is None

    def test_get_buffer_utilisation_empty_queues(self) -> None:
        """Buffer utilisation is 0.0 when all queues are empty."""
        bus = EventBus()
        bus.subscribe()
        bus.subscribe()
        utilisation = bus.get_buffer_utilisation()
        assert len(utilisation) == 2
        for sub_id, ratio in utilisation:
            assert ratio == 0.0
            assert isinstance(sub_id, str)

    async def test_get_buffer_utilisation_partial_fill(self) -> None:
        """Buffer utilisation reflects partial queue fill correctly."""
        from lithos.config import EventsConfig

        config = EventsConfig(subscriber_queue_size=4)
        bus = EventBus(config)
        bus.subscribe()
        # Emit 2 events — queue size 4, so utilisation should be 0.5
        for _ in range(2):
            await bus.emit(LithosEvent(type=NOTE_CREATED))
        utilisation = bus.get_buffer_utilisation()
        assert len(utilisation) == 1
        _, ratio = utilisation[0]
        assert ratio == 0.5

    async def test_get_buffer_utilisation_full_queue(self) -> None:
        """Buffer utilisation is 1.0 when queue is full."""
        from lithos.config import EventsConfig

        config = EventsConfig(subscriber_queue_size=3)
        bus = EventBus(config)
        bus.subscribe()
        # Emit 10 events to overflow the queue of size 3
        for _ in range(10):
            await bus.emit(LithosEvent(type=NOTE_CREATED))
        utilisation = bus.get_buffer_utilisation()
        assert len(utilisation) == 1
        _, ratio = utilisation[0]
        assert ratio == 1.0

    async def test_subscriber_id_consistent_across_drops(self) -> None:
        """The subscriber_id used in drop tracking is stable."""
        from lithos.config import EventsConfig

        config = EventsConfig(subscriber_queue_size=1)
        bus = EventBus(config)
        queue = bus.subscribe()
        sub_id = bus.get_subscriber_id(queue)
        assert sub_id is not None
        # Cause a drop
        for _ in range(5):
            await bus.emit(LithosEvent(type=NOTE_CREATED))
        assert bus.get_drop_count(queue) > 0
        # Subscriber id is still stable
        assert bus.get_subscriber_id(queue) == sub_id

    def test_register_event_bus_metrics_no_raise_without_otel(self) -> None:
        """register_event_bus_metrics must not raise when OTEL is inactive."""
        from lithos.telemetry import register_event_bus_metrics

        bus = EventBus()
        bus.subscribe()
        # Should be a clean no-op when _initialized is False
        register_event_bus_metrics(bus)

    def test_event_bus_subscriber_drops_counter_accessible(self) -> None:
        """lithos_metrics.event_bus_subscriber_drops is always available."""
        from lithos.telemetry import lithos_metrics

        counter = lithos_metrics.event_bus_subscriber_drops
        assert counter is not None
        # Must support .add() without raising
        counter.add(1, {"subscriber_id": "test-id"})


class TestEventTypeConstants:
    """Verify all event type constants are defined."""

    def test_all_event_types_defined(self) -> None:
        from lithos import events

        expected = [
            "note.created",
            "note.updated",
            "note.deleted",
            "task.created",
            "task.claimed",
            "task.released",
            "task.completed",
            "finding.posted",
            "agent.registered",
            "batch.queued",
            "batch.applying",
            "batch.projecting",
            "batch.completed",
            "batch.failed",
        ]
        for event_type in expected:
            # Verify it's a module-level constant
            found = False
            for attr_name in dir(events):
                val = getattr(events, attr_name)
                if val == event_type:
                    found = True
                    break
            assert found, f"Event type {event_type!r} not found as module constant"
