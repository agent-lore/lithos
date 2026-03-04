# source_url as Indexed Field + Dedup on Write

Combines: "URL deduplication field" + "source_url as indexed field"

KnowledgeMetadata already has a `source` field, but it holds a **task ID** (via `source_task` in `lithos_write`), it's not indexed, and nothing checks for collisions. This work makes URL provenance a first-class citizen by adding a **new** `source_url` field alongside the existing `source`.

## Problem

Without dedup, every time a daily research task runs it can write duplicate notes on the same URLs. After a week that's potentially 50+ near-identical documents. Search results get polluted, semantic search has to wade through redundant content, and agents lose confidence in what they find. It compounds silently.

The research cache plan (already designed) depends on `lithos_cache_lookup` finding the one authoritative note for a URL. If there are four duplicate notes with different IDs and slightly different content, the cache is unreliable. Dedup is load-bearing infrastructure for the cache.

## Design

### 1. New `source_url` field on KnowledgeMetadata

Add `source_url: str | None = None` to `KnowledgeMetadata` alongside the existing `source` field (which continues to hold task IDs).

**URL normalization** — before storing or comparing, normalize URLs:
- Lowercase scheme and host (`HTTPS://Example.COM` -> `https://example.com`)
- Strip trailing slash on path (`https://example.com/page/` -> `https://example.com/page`)
- Remove default ports (`:443` for https, `:80` for http)
- Remove fragment (`#section`)
- Sort query parameters alphabetically
- Strip common tracking params (`utm_*`, `ref`, `fbclid`)

Add a `normalize_url(raw: str) -> str` helper in `knowledge.py`. Keep it simple — use `urllib.parse` and a small blocklist of tracking params.

Update `to_dict`, `from_dict`, `create`, and `_scan_existing` to handle the new field.

### 2. In-memory index for O(1) dedup lookups

Add `_source_url_to_id: dict[str, str]` to `KnowledgeManager`, populated during `_scan_existing` alongside the existing `_id_to_path` and `_slug_to_id` maps. This avoids hitting Tantivy on every write.

Maintain the map on create, update, and delete:
- **create**: add entry after successful write
- **update**: if `source_url` changed, remove old mapping, add new one; skip collision check against the document's own ID
- **delete**: remove entry

### 3. Index source_url in Tantivy

Add `source_url` as a keyword field (raw tokenizer, stored) in `TantivyIndex._build_schema()`. This enables exact-match queries like `source_url:https://example.com/page`.

**Schema migration**: adding a field to the Tantivy schema makes existing indices incompatible. The existing `open_or_create` recovery path (delete + recreate on corruption) handles this, but `_rebuild_indices` will run on next startup. Note this in release/upgrade docs.

Include `source_url` in `TantivyIndex.add_document()`:
```python
writer.add_document(
    tantivy.Document(
        id=doc.id,
        title=doc.title,
        content=doc.full_content,
        path=str(doc.path),
        author=doc.metadata.author,
        tags=" ".join(doc.metadata.tags),
        source_url=doc.metadata.source_url or "",
    )
)
```

### 4. Store source_url in ChromaDB metadata

Add `source_url` to the per-chunk metadata dict in `ChromaIndex.add_document()` so semantic search results can also return provenance:
```python
metadatas = [
    {
        "doc_id": doc.id,
        ...
        "source_url": doc.metadata.source_url or "",
    }
    for i in range(len(chunks))
]
```

### 5. Dedup check in lithos_write

Add `source_url: str | None = None` parameter to `lithos_write`.

On create (no `id` provided), if `source_url` is provided:
1. Normalize the URL
2. Look up `_source_url_to_id` for an existing document
3. If found, **do not write**. Return a response with a `duplicate_of` field:

```python
{
    "status": "duplicate",
    "duplicate_of": {
        "id": existing_id,
        "title": existing_title,
        "source_url": normalized_url,
    },
    "message": "A document with this source_url already exists. Use the existing document's id to update it."
}
```

On update (`id` provided), if `source_url` is provided:
1. Normalize the URL
2. Check `_source_url_to_id` — only flag collision if the mapping points to a **different** document ID
3. If collision with another doc, return `duplicate_of` as above
4. If no collision, proceed with update and maintain the map

Default behavior is **reject-with-info** (return the duplicate info, don't write). This is safer than silently writing duplicates. Agents get the existing doc ID and can update it themselves.

### 6. Return source_url in search results

Add `source_url: str = ""` to both result dataclasses:
- `SearchResult` in search.py
- `SemanticResult` in search.py

Populate from Tantivy doc / ChromaDB metadata in the respective `search()` methods.

Include `source_url` in all response dicts in server.py:
- `lithos_search` results
- `lithos_semantic` results
- `lithos_read` response metadata
- `lithos_list` items

### 7. Pass source_url through KnowledgeManager.create

Add `source_url: str | None = None` parameter to `KnowledgeManager.create()`. Normalize before storing. The dedup check itself lives in `lithos_write` (server layer), not in `KnowledgeManager`, so the manager stays a simple CRUD layer.

## Files

| File | Changes |
|------|---------|
| `knowledge.py` | Add `source_url` to `KnowledgeMetadata` (field, `to_dict`, `from_dict`). Add `normalize_url()` helper. Add `_source_url_to_id` map to `KnowledgeManager`. Update `_scan_existing`, `create`, `update`, `delete` to maintain the map. Add `source_url` param to `create`. |
| `search.py` | Add `source_url` keyword field to Tantivy schema. Include in `add_document` for both Tantivy and ChromaDB. Add `source_url` field to `SearchResult` and `SemanticResult`. Populate in both `search()` methods. |
| `server.py` | Add `source_url` param to `lithos_write`. Dedup check before create. Include `source_url` in responses for `lithos_write`, `lithos_read`, `lithos_search`, `lithos_semantic`, `lithos_list`. |
| `tests/` | Test URL normalization. Test dedup on create (duplicate detected, no-collision, update-self). Test source_url appears in search results. Test schema rebuild path. |

## Scope

Medium — touches three source files plus tests. Logic is straightforward but the surface area is wide (every read/search/list response). The Tantivy schema change forces a reindex on upgrade, which should be documented.

## Out of scope (future)

- Configurable `dedup_mode` (warn/reject/upsert) in `LithosConfig` — start with reject-with-info, add config if agents need different behavior
- Bulk dedup check for the bulk-write feature (batch the `_source_url_to_id` lookups)
- Retroactive dedup scan for existing documents that share URLs
