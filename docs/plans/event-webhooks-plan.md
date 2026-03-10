# Basic Webhooks

Contract note: system-level rollout and compatibility constraints are governed by `final-architecture-guardrails.md`. Write-path semantics referenced here are governed by `unified-write-contract.md`.

## Status

Deferred indefinitely. No assigned phase.

## Goal

Add a minimal webhook surface for convenience integrations that cannot hold open SSE connections.

This is intentionally not a guaranteed-delivery system. If delivery fails, it fails. SSE remains the primary path for local agents, and this webhook layer should stay out of the active roadmap until there is a concrete consumer that needs it.

## Dependency

`event-bus-plan.md` must be complete first. `event-sse-plan.md` should be complete before revisiting this because SSE is the primary delivery surface.

## Scope

- one webhook registry table in `coordination.db`
- fire-and-forget HTTP POST delivery for matching events
- HMAC signing for basic authenticity
- two MCP tools: register and delete

## Non-Goals

- no outbox table
- no retry worker
- no restart-safe processing
- no delivery history table
- no dead-letter handling
- no list/deliveries management tools in the baseline plan

## Storage

Add one table to the existing `coordination.db`:

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
```

This schema is intentionally compatible with adding an outbox later.

## Design

### Registration

Add `CoordinationService` methods:

- `register_webhook`
- `delete_webhook`
- `list_matching_webhooks` or equivalent internal query helper

### Delivery

When an event is emitted:

- load matching webhooks by `event_types` and `tags`
- POST JSON payload directly to each target
- include `event.id` in the payload so consumers can dedupe
- include `X-Lithos-Signature: sha256=<hmac>` header
- log failures through normal server logging/telemetry, but do not persist attempts

Delivery must remain off the authoritative write path:

- webhook failures never fail the originating write/task operation
- duplicate deliveries are still possible if callers replay or reconnect elsewhere
- ordering is best-effort only

## MCP Tools

```python
lithos_webhook_register(url, secret, event_types, tags, agent)
  -> {webhook_id}

lithos_webhook_delete(webhook_id, agent)
  -> {success}
```

List/history tools are deferred until there is evidence they are needed.

## Config

Extend `EventsConfig` in `config.py`:

```python
class EventsConfig(BaseModel):
    webhooks_enabled: bool = False
    webhook_timeout_seconds: int = 10
```

Default `webhooks_enabled` to `False` so the feature is opt-in until it has a concrete consumer.

## Files

| File | Change |
| ---- | ------ |
| `src/lithos/coordination.py` | Add `webhooks` table and minimal registry methods |
| `src/lithos/server.py` | Add 2 webhook MCP tools and fire-and-forget dispatch |
| `src/lithos/config.py` | Add webhook enable flag and timeout |
| `tests/test_event_delivery.py` | Add webhook registration and best-effort delivery tests |

## Exit Criteria

- a local or LAN service can register a webhook and receive matching events
- failed webhook calls are isolated from the originating write path
- the implementation remains small enough to retrofit durable delivery later instead of committing to it now
