# Research Cache — Copy

---

## Pull Request Description

### Add research cache: `lithos_cache_lookup`, TTL on documents, freshness in search

#### Summary

Adds a single-call research cache to Lithos. Before doing any external lookup, agents can now call `lithos_cache_lookup` to ask "do you already know this, and is it still fresh?" — and get a direct answer instead of assembling that logic themselves from multiple tool calls.

This also introduces a `expires_at` / `ttl_hours` field on knowledge documents so writing agents can declare how long their findings should be trusted.

---

#### Motivation

In a multi-agent system, research is the most expensive operation. Without a way to check what's already known — and whether it's still valid — agents default to researching from scratch every time. That means duplicate notes, redundant API calls, and a knowledge base that accumulates stale copies instead of converging on updated ones.

The current workaround requires 2–3 tool calls and the agent doing the freshness math itself:

```
lithos_semantic(query) → lithos_read(id) → inspect updated_at → decide
```

This PR makes that a single call where Lithos does the logic.

---

#### Changes

**`knowledge.py`** — Add `expires_at` and `is_stale` to `KnowledgeMetadata`

- New optional `expires_at: datetime | None` field on `KnowledgeMetadata`
- New `is_stale` property: returns `True` if `expires_at` is set and has passed (timezone-safe via existing `_normalize_datetime` utility)
- `to_dict()` / `from_dict()` updated to handle `expires_at` (same pattern as `created_at`/`updated_at`)
- `KnowledgeManager.create()` and `update()` accept `expires_at: datetime | None = None`
- No migration required — `expires_at` is an optional YAML key; existing docs without it remain valid

**`server.py`** — Extend `lithos_write` with TTL support

- Two new optional parameters: `ttl_hours: float | None` and `expires_at: str | None` (ISO datetime string)
- Mutually exclusive — providing both returns an `invalid_input` error
- `ttl_hours` is resolved to an absolute `expires_at` at write time (`now + timedelta(hours=ttl_hours)`)
- Passed through to `knowledge.create()` / `knowledge.update()`

**`server.py`** — Add `lithos_cache_lookup` tool

The main addition. One call, structured hit/miss response:

```python
lithos_cache_lookup(
    query: str,
    source_url: str | None = None,   # exact URL match takes priority over semantic
    max_age_hours: float | None = None,
    min_confidence: float = 0.5,
    limit: int = 3,
    tags: list[str] | None = None,
)
```

Returns:

| Field | Type | Meaning |
|---|---|---|
| `hit` | bool | Fresh, usable knowledge found |
| `document` | dict \| None | Full doc content if hit |
| `stale_exists` | bool | Relevant doc exists but is stale |
| `stale_id` | str \| None | ID of stale doc to update (not duplicate) |

Lookup logic: semantic search → filter by `min_confidence` → check `is_stale` → check `max_age_hours` cutoff. If `source_url` is provided, exact URL match is tried first (fast-path dedup, consistent with `source-url-dedup` contract).

**`search.py`** — Freshness fields in `SearchResult`

- `updated_at` and `is_stale` now carried on `SearchResult` and surfaced in `lithos_search` / `lithos_semantic` results
- `expires_at` already indexed in Tantivy schema; now populated from `KnowledgeMetadata.expires_at`
- Means agents can see freshness from a search response without a follow-up `lithos_read`

---

#### Agent usage

**Before** (current — 3 calls, agent does freshness logic):
```
lithos_semantic(query) → lithos_read(id) → decide manually
```

**After** (1 call):
```
lithos_cache_lookup(query, max_age_hours=24, min_confidence=0.7)
  → hit=True                              → use content, skip research
  → hit=False, stale_exists=True          → research, then lithos_write(id=stale_id, ...)
  → hit=False, stale_exists=False         → research, then lithos_write(ttl_hours=48, ...)
```

The `stale_id` path is the key behaviour: agents update the existing note instead of creating a duplicate, keeping the knowledge base coherent.

---

#### Notes

- Schema change in Tantivy: if the search index was built before `expires_at` was indexed, startup will trigger a full index rebuild (same path as documented in `source-url-dedup.md` for schema incompatibilities)
- Write-path request/response semantics follow `unified-write-contract.md`
- System-level rollout and compatibility guardrails: `final-architecture-guardrails.md`
- `_normalize_datetime` (existing utility in `knowledge.py`) handles timezone normalisation throughout — no new datetime handling introduced

---

---

## Release Note

### `lithos_cache_lookup` — research cache with TTL support

**What's new**

Lithos now has a single-call research cache. Before doing any external lookup, call `lithos_cache_lookup` with your query. Lithos checks whether fresh, high-confidence knowledge already exists and returns a structured hit/miss response — no manual freshness logic required.

When writing research results, you can now attach a time-to-live: `ttl_hours=168` (7 days) or an explicit `expires_at` ISO timestamp. Lithos uses this to decide whether knowledge is still valid on future lookups.

**Why it matters**

In a busy multi-agent system, research is expensive. Without a cache, every agent starts from scratch — burning tokens on lookups that a teammate completed an hour ago. The cache makes re-research a last resort rather than a reflex.

The `stale_id` return value is particularly useful: when relevant knowledge exists but has expired, Lithos tells you *which document to update* instead of letting agents create duplicates. The knowledge base stays coherent rather than accumulating stale copies.

**New tool: `lithos_cache_lookup`**

```
lithos_cache_lookup(query, max_age_hours=24, min_confidence=0.7)
```

Returns `hit=True` with full document content, or `hit=False` with `stale_exists` / `stale_id` guidance for the re-research path.

Optional `source_url` parameter enables exact URL deduplication — useful when agents know the canonical source they're about to fetch.

**Updated: `lithos_write`**

```
lithos_write(..., ttl_hours=168)         # valid for 7 days from now
lithos_write(..., expires_at="2026-03-16T12:00:00Z")  # explicit expiry
```

**Updated: `lithos_search` / `lithos_semantic`**

Search results now include `updated_at` and `is_stale` fields — agents can see freshness without a follow-up read.

**Files changed:** `knowledge.py`, `server.py`, `search.py`  
No data migration required. Existing documents without `expires_at` are treated as non-expiring.
