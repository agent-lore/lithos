# Bulk Write API v3 (Durable Ingestion + Async Projection)

Contract note: item-level request/response semantics in this plan are governed by `unified-write-contract.md`. System-level rollout and compatibility guardrails are governed by `final-architecture-guardrails.md`.

## Goal

Support high-throughput, retry-safe batch writes without sacrificing data integrity.

v3 treats batch ingestion as a durable workflow, not a synchronous loop over `lithos_write`.

## Core Model

1. **Write plane (source of truth):** Markdown/frontmatter files.
2. **Projection plane (derived):** Tantivy, ChromaDB, and graph cache.
3. **Durable workflow state:** Batch journal in a dedicated `data/.lithos/batch.db` SQLite file, consistent with the LCMA pattern of separate SQLite files per concern (`edges.db`, `stats.db`).

Writes must be durable and queryable even when projections are temporarily behind.

## New Tools

## 1) `lithos_write_batch`

Accepts a batch, validates it, persists a durable batch record + items, and enqueues work.

```python
lithos_write_batch(
    documents: list[CreateItem | UpdateItem],
    mode: "best_effort" | "all_or_nothing" = "best_effort",
    idempotency_key: str | None = None,
    priority: "low" | "normal" | "high" = "normal"
) -> {
    "batch_id": str,
    "status": "accepted" | "replayed",
    "summary": {
        "requested": int,
        "accepted": int,
        "rejected_preflight": int
    },
    "preflight_errors": list[ItemError]
}
```

`CreateItem`:
- `title`, `content`, `agent`
- optional: `tags`, `confidence`, `path`, `source_url`, `source_task`, `derived_from_ids`
- optional freshness: `ttl_hours`, `expires_at`
- optional LCMA metadata: `note_type`, `namespace`, `access_scope`, `entities`, `status`, `schema_version`
- optional: `idempotency_key` (per-item override)

Note: until LCMA metadata support lands, LCMA-only fields may be rejected as unsupported input.

`UpdateItem`:
- `id`, `agent`
- optional mutable fields as above
- optional: `if_match_updated_at` or `if_match_hash` (optimistic concurrency)
- optional: `idempotency_key`

### Shared Write Contract (single + batch)

Batch items use the same canonical write contract as `lithos_write`:

- same field set and validation rules
- same manager-level invariants (source URL dedup, provenance normalization, freshness parsing)
- same outcome semantics

Per-item write outcomes in `lithos_batch_status.items[*].result_json` use the status envelope:

```python
{"status": "created", "id": "...", "path": "...", "warnings": []}
{"status": "updated", "id": "...", "path": "...", "warnings": []}
{
  "status": "duplicate",
  "duplicate_of": {"id": "...", "title": "...", "source_url": "..."},
  "message": "A document with this source_url already exists.",
  "warnings": []
}
```

`warnings` covers non-fatal issues such as unresolved `derived_from_ids`.

## 2) `lithos_batch_status`

Returns workflow progress and per-item outcomes.

```python
lithos_batch_status(batch_id: str) -> {
    "batch_id": str,
    "status": "queued" | "applying" | "projecting" | "completed" | "completed_with_errors" | "failed",
    "mode": "best_effort" | "all_or_nothing",
    "summary": {
        "requested": int,
        "write_ok": int,
        "write_failed": int,
        "projection_ok": int,
        "projection_failed": int,
        "pending": int
    },
    "items": list[BatchItemResult]
}
```

## 3) `lithos_batch_list` (optional but recommended)

List recent batches with status, timestamps, and aggregates for operational visibility.

## Idempotency

Idempotency is first-class:

- Batch-level key: exact same request + key returns prior `batch_id` and status `replayed`.
- Item-level key: protects against duplicate item application if a worker retries.
- Store idempotency records with stable request hash and final result payload.

This makes client retries safe under timeouts/network failures.

## Execution Pipeline

## Phase A: Ingestion (synchronous, fast)

1. Validate request size and shape.
2. Validate items (UUID formats, field types, limits).
3. Persist batch and items in SQLite with `queued` status.
4. Enqueue batch for worker.
5. Return `batch_id` quickly.

No embeddings or graph serialization in request path.

## Phase B: Apply writes (worker)

Under manager-owned lock:

1. Resolve dedup decisions (source URL policies).
2. Apply create/update to markdown files.
3. Update manager in-memory indexes/maps.
4. Commit item write outcomes to journal.

For `mode="all_or_nothing"`:
- Stage file changes to temp files.
- If any write-phase item is not applied (`created`/`updated`), including `duplicate`, abort batch write-phase and do not publish staged files.
- Mark all affected items as failed/aborted with explicit codes.
- Use `batch_aborted_non_apply_outcome` for items aborted due to an earlier non-apply outcome in the batch.

For `mode="best_effort"`:
- Apply independently and continue after per-item failures.

## Phase C: Build projections (worker, outside write lock)

For each successful write item:

1. `search.index_document(doc)`
2. `graph.add_document(doc)`

After batch:
- `graph.save_cache()` once.

Projection failures are recorded per item and retried with backoff.

## Consistency Contract

1. Source of truth is the markdown corpus.
2. `lithos_read`/`lithos_list` reflect write-phase success immediately after phase B.
3. Search/graph may lag; status endpoint exposes lag/errors.
4. Reconciliation worker retries failed projections until success or dead-letter threshold.

## Error Taxonomy

Return stable machine-readable codes:

- `invalid_input`
- `invalid_uuid`
- `path_collision`
- `stale_write_conflict`
- `doc_not_found`
- `index_backend_unavailable`
- `graph_update_failed`
- `projection_retry_exhausted`
- `batch_aborted_non_apply_outcome`
- `internal_error`

Human-readable `message` accompanies each code.

`duplicate_source_url` is represented as a write outcome (`status="duplicate"`) in per-item `result_json`, not as a transport-level failure code.

## Concurrency and Safety

- No server access to private manager locks.
- Workers are single-writer for apply phase (or partitioned with strict keying).
- Projection can be parallelized per backend after write phase, with bounded worker pools.
- Dedup invariants remain manager-owned.
- Workers are asyncio tasks (not threads or subprocesses), since all Lithos I/O is async-native and workers need access to the manager's async `_write_lock`. `worker_count` in `BatchConfig` controls concurrent asyncio task workers; for the apply phase (Phase B), only one worker per manager instance executes at a time to enforce single-writer semantics.

## Config

Add:

```python
class BatchConfig(BaseModel):
    max_size: int = 100
    max_total_chars: int = 500_000
    max_single_doc_chars: int = 100_000
    queue_max_pending: int = 1000
    worker_count: int = 2
    projection_retry_max: int = 5
    projection_retry_base_ms: int = 500
```

Behavior:
- Reject batch if limits exceeded before journaling.
- If queue is saturated, reject low-priority requests with backpressure error.

## Observability

Batch observability is implemented via the OTEL foundation in `otel-plan.md` (`telemetry.py`, `traced`, and `lithos_metrics`), not a parallel instrumentation stack.

Required OTEL metrics:

- `lithos.batch.ingest_latency_ms`
- `lithos.batch.queue_depth`
- `lithos.batch.apply_latency_ms`
- `lithos.batch.projection_latency_ms`
- `lithos.batch.projection_lag_ms`
- `lithos.batch.items_failed_total{code=...}`
- `lithos.batch.retries_total`
- `lithos.batch.dead_letter_total`

Required traces:

- `lithos.batch.ingest`
- `lithos.batch.apply`
- `lithos.batch.project`
- `lithos.batch.reconcile`

Structured logs:
- include `batch_id`, item index, status transitions, and error code.

## Data Model (Journal)

Tables (illustrative):

- `batches(id, idempotency_key, mode, priority, status, requested_count, created_at, updated_at, request_hash)`
- `batch_items(id, batch_id, item_index, payload_json, status, write_status, projection_status, result_json, error_code, error_message, retries, updated_at)`
- `idempotency_keys(scope, key, request_hash, response_json, created_at, expires_at)`

## Migration Strategy

1. Implement journal + status tool behind feature flag.
2. Keep existing synchronous `lithos_write` available, but align it and batch on the same status-based response envelope and shared validation/invariant path.
3. Introduce `lithos_write_batch` v3 as opt-in.
4. After soak, make v3 default batch path.

No changes to current read/search/list contracts required.

## Risks

1. Higher implementation complexity than v2.
2. Requires robust worker lifecycle management.
3. Eventual consistency may surprise clients unless status usage is clear.

Mitigations:
- strict status model
- explicit consistency docs
- retries + dead-letter + manual reconcile tool

## Testing

1. Idempotent replay at batch and item levels.
2. Best-effort vs all-or-nothing behavior.
3. Worker crash/restart mid-batch (resume correctness).
4. Projection retry and dead-letter behavior.
5. Stale update detection via `if_match_*`.
6. Queue saturation/backpressure handling.
7. Cross-feature tests with source URL dedup and derived provenance metadata.

## Scope

Large. This is an architecture upgrade, not an endpoint wrapper.

It should be scheduled as a multi-PR effort with feature flags and operational instrumentation from day one.
