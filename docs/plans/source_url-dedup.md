# source_url as Indexed Field + Dedup on Write

Combines: "URL deduplication field" + "source_url as indexed field"

Contract note: `lithos_write` request/response semantics in this plan are governed by `unified-write-contract.md`. System-level rollout and compatibility guardrails are governed by `final-architecture-guardrails.md`. Search schema targets are governed by `target-search-schema.md`.

KnowledgeMetadata already has a `source` field, but it holds a **task ID** (via `source_task` in `lithos_write`), it is not indexed for URL provenance, and no collision checks exist. This work adds a new `source_url` field and enforces URL uniqueness.

## Problem

Without dedup, recurring research tasks can repeatedly write near-identical notes from the same URL. Search gets noisy, semantic retrieval gets redundant chunks, and cache lookup cannot reliably find one authoritative note.

The research cache plan depends on stable URL-to-document mapping. Dedup is foundational for that.

## Design

### 1. New `source_url` field on KnowledgeMetadata

Add `source_url: str | None = None` to `KnowledgeMetadata` alongside existing `source` (which remains task-ID oriented).

Add `normalize_url(raw: str) -> str` in `knowledge.py`:
- Lowercase scheme and host (`HTTPS://Example.COM` -> `https://example.com`)
- Remove fragment (`#section`)
- Remove default ports (`:443` for https, `:80` for http)
- Strip trailing slash when path is not root (`/page/` -> `/page`)
- Sort query params alphabetically
- Remove tracking params (`utm_*`, `fbclid`)

`ref` is **not** removed by default (it can be semantically meaningful on some sites).

Validation:
- Accept only `http` and `https`
- Treat empty/whitespace URL as invalid input

**Normalize on write to disk.** When `create` or `update` stores a `source_url`, normalize it before writing to frontmatter. This ensures the YAML on disk, the in-memory map, and both search indices always hold the same canonical form. No read-time normalization needed; `lithos_read` returns exactly what's on disk. `_scan_existing` still normalizes on load to handle documents written before this feature existed or edited manually.

Update `to_dict`, `from_dict`, `create`, `update`, and `_scan_existing` to round-trip and normalize `source_url`.

### 2. Manager-level dedup invariants (single source of truth)

Dedup enforcement must live in `KnowledgeManager`, not only `server.py`, so all write paths follow the same invariant.

Add:
- `_source_url_to_id: dict[str, str]`
- `asyncio.Lock` (e.g., `_write_lock`) to protect check+write critical sections

Maintain `_source_url_to_id` in `_scan_existing`, create, update, and delete.

Sentinel for omit-vs-clear: Python cannot distinguish `source_url=None` (caller wants to clear) from an omitted keyword argument (caller wants to preserve). Use a module-level sentinel:

```python
_UNSET = object()
```

Then signature becomes `source_url: str | None | object = _UNSET`. At call sites:
- `_UNSET` (default) → preserve existing value, no map change
- `None` → clear existing value, remove from map
- `str` → normalize and apply dedup check

Behavior:
- Create with `source_url`: normalize, check map, reject if mapped to another doc
- Update with `source_url` (str): normalize, allow same-doc URL, reject if mapped to different doc
- Update with omitted `source_url` (`_UNSET`): preserve existing value
- Update with `source_url=None`: clear existing value and remove from map

### 2b. `find_by_source_url()` lookup helper

Add `async find_by_source_url(self, url: str) -> KnowledgeDocument | None` to `KnowledgeManager`.

Behavior:

1. Normalize the input URL via `normalize_url()`.
2. Look up the normalized URL in `_source_url_to_id`.
3. If found, call `self.read(id=doc_id)` and return the document.
4. If not found, return `None`.

This is the fast-path lookup used by `lithos_cache_lookup` in `research-cache-plan.md` when the caller provides a `source_url`. It skips full-text and semantic search entirely.

This method is read-only on `_source_url_to_id` and does not need to acquire the write lock; the dict is only mutated under the write lock in create/update/delete operations.

### 3. Duplicate handling and `lithos_write` contract

Since breaking contract is acceptable, make response shape explicit and status-based:

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

`lithos_write` continues to pass `source_task` to `source` and now also accepts `source_url`.

### 4. In-memory index initialization and duplicate audit

`_scan_existing` should:
- Normalize any discovered `source_url`
- Populate `_source_url_to_id`
- Record URL collisions found in existing data and log them deterministically

Startup should not fail on existing collisions, but they must be visible in logs. New writes must enforce dedup strictly.

### 5. Index `source_url` in Tantivy

Add `source_url` as a raw/stored field in `TantivyIndex._build_schema()`.

Add `source_url` in `add_document()`.

Querying guidance:
- Do not rely on naive unquoted query strings such as `source_url:https://...`
- Use quoted/escaped terms or dedicated term-query helper for exact URL match

### 6. Schema migration and rebuild behavior

Adding Tantivy field changes schema. Index recreation alone is not enough; a full content reindex must run immediately after detecting schema incompatibility.

Plan:
- Detect schema-open failure in `open_or_create()`
- Recreate Tantivy index
- Trigger full `_rebuild_indices` (or equivalent) on startup so full-text index is repopulated in same boot

Document this in upgrade notes.

### 7. Store `source_url` in ChromaDB metadata

Add `source_url` to chunk metadata in `ChromaIndex.add_document()` so semantic results include provenance without fetching docs separately.

### 8. Return `source_url` in read/search/list responses

Add `source_url: str = ""` to:
- `SearchResult`
- `SemanticResult`

Populate from Tantivy/Chroma metadata.

Include `source_url` in:
- `lithos_read` metadata
- `lithos_search` results
- `lithos_semantic` results
- `lithos_list` items

## Files

| File | Changes |
|------|---------|
| `knowledge.py` | Add `source_url` to `KnowledgeMetadata`; add `normalize_url`; add `_source_url_to_id`; add write lock; enforce dedup in manager create/update; update `_scan_existing` with duplicate audit logging; keep indices in sync on delete; add `find_by_source_url()` async lookup helper (normalizes input, looks up in `_source_url_to_id` map, returns document or None). |
| `search.py` | Add Tantivy `source_url` field; include in indexed docs; include in Chroma metadata; add `source_url` on `SearchResult`/`SemanticResult`; populate in search methods. |
| `server.py` | Add `source_url` param to `lithos_write`; use status-based return contract (`created`/`updated`/`duplicate`); include `source_url` in read/search/semantic/list responses. |
| `tests/` | Add normalization tests; invalid URL tests; dedup tests for create/update/self-update/clear; concurrent create collision test; existing duplicate-at-startup audit test; search result provenance test; schema migration + immediate rebuild test. |

## Scope

Medium+ — still focused, but includes API contract change, migration behavior, and concurrency safeguards. Surface area spans write path, metadata, two search backends, startup behavior, and tool responses.

## Out of scope (future)

- Configurable `dedup_mode` (`reject`, `warn`, `upsert`) in `LithosConfig`
- Bulk dedup API for batch writes
- Auto-remediation for legacy duplicate URLs (beyond startup audit logging)
