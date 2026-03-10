# Event System — Introduction

Lithos is designed as a shared brain for a heterogeneous fleet of agents, but a shared brain that agents must actively poll for updates is a bottleneck rather than a backbone. Polling introduces latency between when knowledge is written and when other agents act on it, forces every agent to maintain its own scheduling logic, and generates unnecessary load on the server regardless of whether anything has changed. The event system replaces this with a push model: when anything meaningful happens in Lithos — a note is created, a task is claimed, a finding is posted — all interested parties are notified immediately. Two delivery mechanisms are provided because agents live in different environments. SSE (Server-Sent Events) serves agents that maintain a persistent connection to Lithos, such as a local Agent Zero instance that can hold an open HTTP stream and react in real time. Webhooks serve agents that cannot or should not maintain persistent connections — an OpenClaw instance on a separate server, an n8n workflow, or any external system that simply needs Lithos to POST to a URL when something relevant occurs. Both mechanisms share the same internal event bus, so the filtering, payload structure, and delivery guarantees are consistent regardless of how an agent chooses to receive events. The deeper purpose is to enable emergent coordination: agents do not need to know about each other or be explicitly orchestrated — they declare what they care about, and Lithos connects the dots.

## Design Plan

The delivery surface is now split into three layers that share the same internal event bus without needing to ship together:

- `event-sse-plan.md`: the primary real-time path and the only part currently assigned to a phase
- `event-webhooks-plan.md`: a minimal optional webhook layer, deferred indefinitely
- `event-guaranteed-delivery-plan.md`: durable delivery infrastructure, also deferred indefinitely

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                   LithosServer                      │
│                                                     │
│  MCP Tools ──► EventBus ──► SSE clients (push)      │
│  File Watcher ──►   │    └─► Webhook dispatcher     │
│                     │           └─► HTTP POST        │
│                     └─► In-memory event log          │
└─────────────────────────────────────────────────────┘
```

One `EventBus` instance, multiple downstream delivery layers, all events flow through the same path.

## Event Types

```
note.created      note.updated      note.deleted
task.created      task.claimed      task.released      task.completed
finding.posted
agent.registered
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

## New File: `src/lithos/events.py`

This is the core new module. Responsibilities:

### `EventBus` class

- `emit(event)` — fans out to all SSE queues + enqueues webhook deliveries
- `subscribe()` → returns an `asyncio.Queue` for SSE clients
- `unsubscribe(queue)` — cleanup on disconnect
- `_webhook_worker()` — background task that drains webhook delivery queue
- Maintains in-memory ring buffer of last N events (for replay on reconnect)

### Webhook delivery

- Load registered webhooks from DB
- Filter by `event_types` and `tags` per webhook
- POST with `X-Lithos-Signature: sha256=<hmac>` header
- 3 retries, exponential backoff (1s, 4s, 16s)
- Log delivery attempts to SQLite

## Modified: `src/lithos/coordination.py`

Add webhook storage to the existing SQLite database (no new file):

```sql
CREATE TABLE webhooks (
    id          TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    secret      TEXT NOT NULL,
    event_types TEXT,          -- JSON array, NULL = all
    tags        TEXT,          -- JSON array, NULL = all
    created_by  TEXT,
    created_at  TEXT,
    active      INTEGER DEFAULT 1
);

CREATE TABLE webhook_deliveries (
    id          TEXT PRIMARY KEY,
    webhook_id  TEXT,
    event_id    TEXT,
    status      TEXT,          -- 'delivered', 'failed', 'retrying'
    attempts    INTEGER,
    last_attempt TEXT,
    response_code INTEGER
);
```

Add `CoordinationService` methods: `register_webhook`, `list_webhooks`, `delete_webhook`, `log_delivery`.

## Modified: `src/lithos/server.py`

### 1. SSE Endpoint

FastMCP exposes its underlying Starlette app. Mount an additional route:

```python
from starlette.responses import EventSourceResponse

# In LithosServer.__init__ or initialize():
self.mcp.custom_route("/events", self._sse_endpoint, methods=["GET"])
```

The endpoint:

```
GET /events
  ?types=note.created,task.completed   (optional filter)
  ?tags=research,pricing               (optional filter)
  ?since=<event-id>                    (replay from event ID)
```

Returns `text/event-stream`. Each event:

```
id: <event-uuid>
event: note.created
data: {"agent": "az", "title": "Acme Pricing", "id": "...", "tags": ["pricing"]}
```

### 2. Emit events from existing MCP tools

In each tool handler, after the operation succeeds, call `self.event_bus.emit(...)`. Example in `lithos_write`:

```python
await self.event_bus.emit(LithosEvent(
    type="note.created" if not id else "note.updated",
    agent=agent,
    payload={"id": doc.id, "title": doc.title, "path": str(doc.path)},
    tags=doc.metadata.tags,
))
```

Also emit from the file watcher's `handle_file_change` for external edits (e.g. someone editing in Obsidian).

### 3. New MCP tools for webhook management

```python
lithos_webhook_register(url, secret, event_types, tags, agent)
  → {webhook_id}

lithos_webhook_list(agent)
  → {webhooks: [{id, url, event_types, tags, active}]}

lithos_webhook_delete(webhook_id, agent)
  → {success}

lithos_webhook_deliveries(webhook_id, limit)
  → {deliveries: [{event_id, status, attempts, last_attempt}]}
```

## Modified: `src/lithos/config.py`

Add an `EventsConfig` section:

```python
@dataclass
class EventsConfig:
    enabled: bool = True
    sse_enabled: bool = True
    webhooks_enabled: bool = True
    max_sse_clients: int = 50
    event_buffer_size: int = 500      # in-memory ring buffer
    webhook_timeout_seconds: int = 10
    webhook_max_retries: int = 3
```

## Files Changed Summary

| File | Type | Change |
|------|------|--------|
| `src/lithos/events.py` | New | `LithosEvent`, `EventBus`, webhook dispatcher |
| `src/lithos/coordination.py` | Modify | Add `webhooks` + `webhook_deliveries` tables and methods |
| `src/lithos/server.py` | Modify | Wire `EventBus`, add `/events` SSE route, add 4 webhook MCP tools, emit events from all write tools |
| `src/lithos/config.py` | Modify | Add `EventsConfig` dataclass |
| `tests/test_events.py` | New | Tests for `EventBus`, SSE, webhook delivery |

## Usage Patterns

### Agent Zero: polling → event-driven

**Before:** Agent Zero scheduler polls `lithos_list(since=last_check)` every 5 min

**After:** Agent Zero connects to `GET /events?types=note.created,finding.posted` and reacts immediately

### OpenClaw webhook

```python
lithos_webhook_register(
    url="http://openclaw:18789/hooks/agent",
    secret="shared-secret",
    event_types=["note.created", "task.completed"],
    tags=["ready-to-distribute"],
    agent="openclaw"
)
```

OpenClaw gets a POST the moment Agent Zero writes a note tagged `ready-to-distribute`. No polling, no n8n.

### Obsidian user edits

The file watcher already detects manual edits. With the event bus wired in, editing a note in Obsidian fires `note.updated` to all SSE clients and webhooks automatically — no agent involvement needed.

## Open Questions to Decide Before Building

| Question | Options |
|----------|---------|
| SSE auth | Open (local infra) vs bearer token (same as MCP) |
| FastMCP route mounting | `custom_route()` API vs separate Starlette/FastAPI app on second port |
| Webhook delivery | Fire-and-forget background task vs persistent queue (SQLite-backed) |
| Event persistence | Ring buffer only (lost on restart) vs append to SQLite |

For a local-first tool I'd lean toward: open SSE, `custom_route()` mounting, SQLite-backed delivery queue (so retries survive restarts), ring buffer for SSE replay.
