# Internal Event Bus

Contract note: system-level rollout and compatibility constraints are governed by `final-architecture-guardrails.md`. Write-path semantics referenced here are governed by `unified-write-contract.md`.

## Goal

Add an internal event bus that emits on all write/task/delete/finding success paths. The bus is internal infrastructure — no new MCP tools, no SSE endpoint, no webhooks. Later phases and the delivery plan (`event-delivery-plan.md`) consume from this bus.

The bus exists so that:

- later phases can react to state changes without polling or ad-hoc callbacks
- bulk writes (Phase 5) can emit batch status events internally
- LCMA (Phase 7) can trigger consolidation on `lithos_task_complete`
- the delivery surface (Phase 6.5) has a stable emission layer to build on

## Event Types

```
note.created      note.updated      note.deleted
task.created      task.claimed      task.released      task.completed
finding.posted
agent.registered
batch.queued      batch.applying    batch.projecting
batch.completed   batch.failed
```

Each event carries:

```python
@dataclass
class LithosEvent:
    id: str                    # uuid
    type: str                  # e.g. "note.created"
    timestamp: datetime
    agent: str | None          # who triggered it
    payload: dict              # type-specific data
    tags: list[str]            # from the affected document/task
```

## Design

### New file: `src/lithos/events.py`

#### `EventBus` class

- `async emit(event)` — fans out to all subscriber queues
- `subscribe(filter)` → returns an `asyncio.Queue` for internal consumers
- `unsubscribe(queue)` — cleanup
- In-memory ring buffer of last N events (configurable via `EventsConfig.event_buffer_size`)

The bus is purely in-memory. No persistence, no HTTP, no SQLite. Subscribers are internal Python consumers (other Lithos subsystems), not external agents. External delivery is handled by `event-delivery-plan.md`.

#### Subscriber filtering

`subscribe()` accepts an optional filter (event types and/or tags). The bus only enqueues events that match the filter into that subscriber's queue. This keeps consumers from having to filter themselves.

#### Backpressure and failure isolation

The event bus must not turn subscriber slowness into write-path instability.

Required semantics:

- each subscriber queue is bounded (`EventsConfig.subscriber_queue_size`)
- `emit()` never blocks indefinitely on a slow subscriber
- if a subscriber queue is full, the bus drops the event for that subscriber, increments a drop counter, and continues fan-out
- subscriber failures never fail the underlying write/task/batch operation that produced the event
- event emission is best-effort infrastructure, not part of the authoritative commit contract

This keeps writes, task completion, and batch transitions responsive even when an internal consumer is stalled or buggy.

### Config: `EventsConfig` in `config.py`

```python
class EventsConfig(BaseModel):
    """Event bus configuration."""

    enabled: bool = True
    event_buffer_size: int = 500      # in-memory ring buffer size
    subscriber_queue_size: int = 100
```

Only bus-relevant config. SSE and webhook config are added later by `event-delivery-plan.md`.

Add to `LithosConfig`:

```python
class LithosConfig(BaseSettings):
    # ... existing fields ...
    events: EventsConfig = Field(default_factory=EventsConfig)
```

### Emission points in `server.py`

After each successful operation, emit an event. Events are emitted **after** the operation succeeds (DB commit, file write) but **before** returning to the caller.

| Tool | Event type | Payload |
| ---- | ---------- | ------- |
| `lithos_write` (create) | `note.created` | `id`, `title`, `path` |
| `lithos_write` (update) | `note.updated` | `id`, `title`, `path` |
| `lithos_delete` | `note.deleted` | `id`, `path` |
| `lithos_task_create` | `task.created` | `task_id`, `title` |
| `lithos_task_claim` | `task.claimed` | `task_id`, `agent`, `aspect` |
| `lithos_task_release` | `task.released` | `task_id`, `agent`, `aspect` |
| `lithos_task_complete` | `task.completed` | `task_id`, `agent` |
| `lithos_finding_post` | `finding.posted` | `finding_id`, `task_id`, `agent` |
| `lithos_agent_register` | `agent.registered` | `agent_id`, `name` |
| `handle_file_change` (watcher) | `note.updated` / `note.deleted` | `path` |

**Thread-safety note for `handle_file_change`:** The watchdog observer runs `FileSystemEventHandler` callbacks on OS threads, not in the asyncio event loop. `EventBus.emit()` is a coroutine and cannot be called directly from a thread context. The file watcher must bridge to the event loop using `asyncio.run_coroutine_threadsafe(self.event_bus.emit(...), self._watch_loop)`, where `self._watch_loop` is the event loop reference already stored on `LithosServer` (line 39 of `server.py`).

Example emission in `lithos_write`:

```python
await self.event_bus.emit(LithosEvent(
    type="note.created" if not id else "note.updated",
    agent=agent,
    payload={"id": doc.id, "title": doc.title, "path": str(doc.path)},
    tags=doc.metadata.tags,
))
```

Phase 5 extends emission points with batch workflow events:

| Producer | Event type | Payload |
| ---- | ---------- | ------- |
| `lithos_write_batch` ingest | `batch.queued` | `batch_id`, `requested`, `mode`, `priority` |
| batch worker apply start | `batch.applying` | `batch_id` |
| batch worker projection start | `batch.projecting` | `batch_id`, `write_ok` |
| batch worker success | `batch.completed` | `batch_id`, `summary`, `status` |
| batch worker terminal failure | `batch.failed` | `batch_id`, `status`, `error_code` |

Batch events are emitted from the durable workflow transitions in `bulk-write-v3.md`, not inferred from ad-hoc logging.

### Lifecycle

`EventBus` is created in `LithosServer.__init__` and stored as `self.event_bus`. No explicit shutdown is needed at this stage (in-memory only, no background workers).

If `EventsConfig.enabled` is `False`, the bus is still created but `emit()` is a no-op (same pattern as OTEL no-op mode).

Ordering and delivery semantics:

- event emission happens only after the authoritative operation succeeds
- event order is best-effort process-local order, not a transactional cross-subsystem guarantee
- consumers must treat `event.id` as the stable deduplication key

## Files

| File | Change |
| ---- | ------ |
| `src/lithos/events.py` | New: `LithosEvent`, `EventBus` with ring buffer and subscriber management |
| `src/lithos/config.py` | Add `EventsConfig` (bus fields only) to `LithosConfig` |
| `src/lithos/server.py` | Create `EventBus` in `__init__`, add `emit()` calls in all tool handlers and file watcher |
| `src/lithos/batch.py` or worker module | Emit batch lifecycle events from durable status transitions |
| `tests/test_event_bus.py` | New: emit/subscribe/filter/ring-buffer/no-op-when-disabled tests |

## Out of Scope

The following are covered by `event-delivery-plan.md` (Phase 6.5):

- SSE endpoint (`GET /events`)
- Webhook registry and dispatcher
- Webhook MCP tools (`lithos_webhook_register`, etc.)
- HMAC signing, delivery retries, SQLite delivery log
- `max_sse_clients`, `webhook_timeout_seconds`, `webhook_max_retries` config
