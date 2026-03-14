# Guaranteed Webhook Delivery

Contract note: system-level rollout and compatibility constraints are governed by `final-architecture-guardrails.md`. Write-path semantics referenced here are governed by `unified-write-contract.md`.

## Status

Deferred indefinitely. No assigned phase.

This plan exists to preserve the durable-delivery design, not to define current implementation scope.

## Why Deferred

The full design solves for failures that do not currently justify the complexity:

- server restart during webhook processing
- slow or intermittently unavailable consumers
- retry scheduling and dead-letter handling
- audit history for each delivery attempt
- duplicate suppression across workers

Current expected consumers are local and already better served by SSE. Treating webhooks as convenience delivery keeps the implementation proportional to actual needs.

## When To Revisit

Revisit this plan only if one or more of these become true:

- Lithos has external consumers that cannot use SSE
- missed webhook deliveries become operationally important
- delivery history is required for debugging or support
- the server must continue delivery after restart without losing queued events

## Deferred Design

### Storage

Add durable queueing and audit tables to `coordination.db`:

```sql
CREATE TABLE webhook_outbox (
    id              TEXT PRIMARY KEY,
    webhook_id      TEXT NOT NULL,
    event_id        TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    status          TEXT NOT NULL,    -- 'pending', 'retrying', 'delivered', 'dead_letter'
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    locked_at       TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE webhook_deliveries (
    id            TEXT PRIMARY KEY,
    outbox_id     TEXT,
    webhook_id    TEXT,
    event_id      TEXT,
    status        TEXT,
    attempts      INTEGER,
    last_attempt  TEXT,
    response_code INTEGER
);
```

### Worker behavior

- enqueue webhook deliveries durably from the event bus
- run a background worker that claims due outbox rows
- prevent double-processing with claim/lock semantics
- retry with exponential backoff such as `1s`, `4s`, `16s`
- mark terminal failures as dead-letter
- write every attempt to delivery history

### MCP surface

If this plan is activated later, add:

- `lithos_webhook_list`
- `lithos_webhook_deliveries`

Current baseline only needs register/delete.

## Constraints

- this remains downstream of the internal event bus
- it does not upgrade Lithos to exactly-once delivery
- consumers must still dedupe by `event.id`

## Exit Criteria For Future Adoption

- webhook delivery survives full server restart
- retry and dead-letter behavior are deterministic and test-covered
- multi-worker or crash-recovery semantics are clearly defined, not implied
