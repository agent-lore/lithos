# Bulk Write API (lithos_write_batch)

**Prerequisite:** [source_url-dedup.md](source_url-dedup.md) must be implemented first. This plan assumes `source_url`, `normalize_url`, `_source_url_to_id`, and the `_write_lock` all exist.

## Problem

The daily task made 8 separate `lithos_write` calls to store findings — each is a round-trip through the MCP protocol, per-document search indexing, and a full `graph.save_cache()` serialization. A batch endpoint collapses this into a single call with one graph save.

## Design

```python
lithos_write_batch(
    documents: list[{title, content, agent, tags, confidence, path, source_url, source_task}],
    on_duplicate: "skip" | "update" | "error"
) → {created: [...], updated: [...], skipped: [...], errors: [...]}
```

### 1. Indexing strategy: incremental adds, single graph save

The batch loop calls `search.index_document()` and `graph.add_document()` per document (these are cheap incremental operations). `graph.save_cache()` is called once at the end — this is where the real per-call overhead lives today, as it serializes the entire NetworkX graph to disk.

This is **not** a full `_rebuild_indices()` (which clears all indices and re-scans every file on disk). A full rebuild would be slower than the unbatched path.

### 2. Failure semantics: best-effort with full reporting

The batch uses **best-effort** semantics. Each document is processed independently. If document 5 of 8 fails (path traversal error, disk full, slug collision, etc.), documents 1–4 remain written and indexed, and documents 6–8 are still attempted.

The response reports the outcome per document:

```python
{
    "created": [{"id": "...", "path": "...", "title": "..."}],
    "updated": [{"id": "...", "path": "...", "title": "..."}],
    "skipped": [{"title": "...", "source_url": "...", "duplicate_of": "..."}],
    "errors":  [{"title": "...", "error": "..."}]
}
```

Callers must inspect the response to detect partial failures. The tool docstring must document this behavior explicitly.

### 3. Batch size limit

Add `batch.max_size: int = 50` to `LithosConfig`. Reject requests exceeding this limit upfront with a clear error before writing anything.

### 4. Write lock: hold for entire batch

The `_write_lock` from the dedup feature must be held for the entire batch, not acquired/released per document. This prevents concurrent single `lithos_write` calls from creating URL collisions mid-batch.

```python
async with self.knowledge._write_lock:
    for doc_spec in documents:
        # dedup check + create/update
    self.graph.save_cache()
```

### 5. `on_duplicate` behavior

Duplicate detection uses `source_url` via the `_source_url_to_id` map from the dedup feature. A document is a "duplicate" when its normalized `source_url` maps to an existing document ID.

| `on_duplicate` | Existing doc found | No existing doc |
| --- | --- | --- |
| `"skip"` | Add to `skipped`, do not write | Create normally |
| `"update"` | Update the existing doc in place | Create normally |
| `"error"` | Add to `errors`, do not write | Create normally |

Documents without a `source_url` are never considered duplicates (they always create).

**Intra-batch duplicates:** If two documents in the same batch share a `source_url`, the first one wins. The second is handled according to `on_duplicate` as if the first had already been persisted.

### 6. `id` parameter handling

If a document in the batch includes an `id`, it is treated as an update (same as `lithos_write` today). The `on_duplicate` check applies only to `source_url`, not `id`. Both can coexist: a document with both `id` and `source_url` updates the given ID and the URL map is kept in sync.

## Files

| File | Changes |
| ------ | --------- |
| `server.py` | Register `lithos_write_batch` tool; orchestration loop with incremental indexing and single `graph.save_cache()`. |
| `knowledge.py` | Optional `create_batch()` / `update_batch()` helpers to keep server.py thin; lock-holding logic. |
| `config.py` | Add `batch.max_size` setting. |
| `tests/` | Batch happy path; partial failure; batch size limit rejection; intra-batch dedup; concurrent batch vs single write; all three `on_duplicate` modes. |

## Scope

Small-to-medium — the core is a wrapper around existing create/update logic, but includes config, lock semantics, intra-batch dedup, and test coverage.
