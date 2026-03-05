# Bulk Write API (`lithos_write_batch`) v2

## Prerequisites

- [source_url-dedup.md](source_url-dedup.md) is implemented.
- `lithos_write` uses the status-based response contract (`created` / `updated` / `duplicate`).

This plan assumes manager-level dedup invariants already exist and are the source of truth.

## Problem

Agents often write many notes in bursts. Repeated single-document calls multiply protocol overhead and repeatedly serialize graph cache state. A batch endpoint should reduce overhead without weakening data integrity.

## API

```python
lithos_write_batch(
    documents: list[{
        title, content, agent, tags, confidence, path, id, source_url, source_task
    }],
    on_duplicate: "skip" | "error" = "skip"
) -> {
    "summary": {"requested": int, "applied": int, "failed": int, "skipped": int},
    "results": [
        {
            "index": int,
            "title": str,
            "status": "created" | "updated" | "skipped" | "error",
            "id": str | None,
            "path": str | None,
            "duplicate_of": {"id": str, "source_url": str} | None,
            "warnings": list[str],
            "write_status": "ok" | "error",
            "index_status": "ok" | "error" | "skipped",
            "graph_status": "ok" | "error" | "skipped",
            "error": str | None,
        }
    ]
}
```

`on_duplicate="update"` is intentionally removed to avoid implicit retargeting to a different document ID.

## Semantics

### 1. Deterministic identity and duplicate rules

- If `id` is provided, it is authoritative.
- If `source_url` collides with another doc ID:
  - `on_duplicate="skip"` => item is skipped.
  - `on_duplicate="error"` => item errors.
- The system never silently changes target ID based on `source_url` collision.
- Items without `source_url` are never URL-deduped.

Intra-batch collisions:
- Process in request order.
- Earlier successful items affect later dedup decisions.

### 2. Two-phase execution model

Phase A (write phase, manager-owned lock):
- Validate request shape and per-item fields.
- Enforce dedup and write markdown files.
- Update manager in-memory maps.

Phase B (projection phase, outside write lock):
- For each successful write, call `search.index_document(doc)` and `graph.add_document(doc)`.
- Call `graph.save_cache()` once after all graph updates.

Rationale:
- avoids lock contention from slow embedding/indexing.
- avoids server reaching into manager private locks.

### 3. Failure model: best effort with explicit phase reporting

Writes are best effort per item.

- A write success followed by indexing failure is reported as partial success (`write_status=ok`, `index_status=error`).
- Batch does not roll back already-written files.
- Batch continues after per-item failures.

At batch end:
- If any projection failures occurred, return warnings and include failed IDs in results for optional later reindex/reconcile.

### 4. Path/slug collision policy (must be explicit)

Current create behavior can overwrite files if title/path collide. Batch must define this explicitly.

Policy for v2:
- Default: reject path collision as per-item error (`error="path collision"`).
- Optional future mode: auto-suffix filenames (`-2`, `-3`) if desired.

### 5. Agent registration behavior

Per item, run `ensure_agent_known(agent)` before write.

- Unknown/invalid agent handling is per-item failure, not batch-fatal.
- Batch continues processing remaining items.

### 6. Limits and guardrails

Add `BatchConfig`:

```python
class BatchConfig(BaseModel):
    max_size: int = 50
    max_total_chars: int = 200_000
```

Attach to `LithosConfig` as `batch: BatchConfig`.

Preflight rejection (no writes occur) when:
- `len(documents) > max_size`
- combined payload size exceeds `max_total_chars`

## Internal Architecture

### 1. Manager API

Add a manager-owned orchestration method (name illustrative):

`KnowledgeManager.apply_batch(documents, on_duplicate) -> list[BatchWriteOutcome]`

Responsibilities:
- lock ownership
- dedup decisions
- create/update execution
- map/index consistency in manager

Server responsibilities:
- call `apply_batch`
- run phase B projections (search/graph)
- shape MCP response

### 2. No private lock access from server

`server.py` must not use `self.knowledge._write_lock` directly.
Locking stays encapsulated inside `KnowledgeManager`.

## Files

| File | Changes |
|------|---------|
| `server.py` | Add `lithos_write_batch`; call manager batch API; perform projection phase; single `graph.save_cache()`; return per-item result objects and summary. |
| `knowledge.py` | Add `apply_batch` + outcome dataclass(es); enforce dedup/path-collision rules under manager lock; keep in-memory maps consistent. |
| `config.py` | Add `BatchConfig` and `LithosConfig.batch`. |
| `docs/SPECIFICATION.md` | Document batch API, limits, best-effort semantics, and per-item status model. |
| `tests/` | Happy path, per-item failures, preflight limit rejection, intra-batch dedup ordering, `id`+`source_url` conflict policy, path collision rejection, projection partial failures, concurrent batch vs single write. |

## Scope

Medium. The API is straightforward, but robust semantics require clear ownership boundaries (manager lock ownership), explicit collision policy, and phase-aware result reporting.
