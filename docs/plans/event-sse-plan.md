# SSE Event Delivery

Contract note: system-level rollout and compatibility constraints are governed by `final-architecture-guardrails.md`. Write-path semantics referenced here are governed by `unified-write-contract.md`.

## Goal

Expose the internal event bus over SSE so local agents can react to note/task/finding changes without polling.

This is the immediate Phase 6.5 deliverable. It is intentionally small and should ship as designed.

## Dependency

`event-bus-plan.md` must be complete first. `EventBus` and `LithosEvent` must already exist and emit on all write/task/delete success paths.

## Scope

- add `GET /events` on the existing FastMCP app
- stream events from the existing in-memory `EventBus`
- support type/tag filtering
- support replay from the in-memory ring buffer by `event.id`
- inherit the MCP auth boundary when auth exists

## Non-Goals

- no persistence across full server restarts
- no webhook delivery
- no durable outbox
- no retry semantics or delivery audit log

## Design

### Route

Mount an additional route on the FastMCP Starlette app:

```python
self.mcp.custom_route("/events", self._sse_endpoint, methods=["GET"])
```

Request shape:

```
GET /events
  ?types=note.created,task.completed
  ?tags=research,pricing
  ?since=<event-id>
```

Reconnect behavior:

- support `Last-Event-ID` header as the primary replay mechanism
- support `?since=<event-id>` as a manual fallback
- when both are present, `Last-Event-ID` wins

### Event format

Return `text/event-stream`. Each event:

```
id: <event-uuid>
event: note.created
data: {"agent": "az", "title": "Acme Pricing", "id": "...", "tags": ["pricing"]}
```

### Server behavior

- each connected client subscribes to `EventBus` with its filter
- replay comes from the existing ring buffer in `events.py`
- live delivery uses the subscriber queue returned by `EventBus.subscribe()`
- disconnect cleanup must call `unsubscribe()`
- `max_sse_clients` should cap concurrent streams

### Auth

SSE must inherit the same auth boundary as MCP:

- if MCP transport is authenticated, `GET /events` uses the same bearer/session policy
- if the server is running in unauthenticated local-only mode, `GET /events` may be open

## Delivery Semantics

- SSE is downstream of the internal bus and never participates in write success/failure
- ordering is best-effort process-local order
- replay is best-effort from the in-memory ring buffer only
- consumers must dedupe by `event.id`
- clients must tolerate missed replay across full server restart

## Config

Extend `EventsConfig` in `config.py`:

```python
class EventsConfig(BaseModel):
    enabled: bool = True
    event_buffer_size: int = 500
    subscriber_queue_size: int = 100

    sse_enabled: bool = True
    max_sse_clients: int = 50
```

## Files

| File | Change |
| ---- | ------ |
| `src/lithos/server.py` | Add `/events` SSE route and stream handler |
| `src/lithos/events.py` | Reuse existing subscribe/filter/ring-buffer helpers |
| `src/lithos/config.py` | Add `sse_enabled` and `max_sse_clients` |
| `tests/test_event_delivery.py` | Add SSE integration, replay, and filtering tests |

## Exit Criteria

- Agent Zero can replace polling with a persistent SSE stream
- Obsidian-driven note updates reach connected agents in real time
- reconnect with `Last-Event-ID` replays from the ring buffer when available
