# Search and Retrieval Reference

## Four Search Tools — Full Decision Matrix

| Scenario | Tool |
|----------|------|
| "Does a doc about X already exist?" (before writing) | `lithos_cache_lookup(query="X")` |
| "Does a doc from this URL exist?" | `lithos_cache_lookup(source_url="https://...")` |
| "Find docs about X" (browsing/exploring) | `lithos_search(query="X")` |
| "Find docs about X in project Y" | `lithos_list(content_query="X", tags=["project:Y"])` |
| "Get the best knowledge for task Z" | `lithos_retrieve(query="...", task_id="Z")` |
| "What's related to this document?" | `lithos_related(id="<uuid>")` |

---

## `lithos_cache_lookup` — "Does this already exist?"

Use **before creating new knowledge**, especially when you have a source URL:

```
lithos_cache_lookup(
    query="transformer attention mechanisms",
    source_url="https://example.com/article",   # exact dedup via normalized URL index
    max_age_hours=168,                           # 1 week
    min_confidence=0.5
)
```

- With `source_url`: exact lookup via URL index — instant, no search involved
- Without `source_url`: falls back to semantic search (catches everything)
- Only evaluates up to `limit` candidates (default 3) — quick existence check, not comprehensive
- **Three outcomes**: `hit=True` (fresh, returns full content), `stale_exists=True` (expired, returns `stale_id`), clean miss
- **Key pattern**: If `stale_exists=True`, pass `stale_id` as `id` to `lithos_write` to UPDATE rather than create a duplicate

---

## `lithos_search` — "Find documents matching this query"

Use for **exploratory discovery** — quick keyword or semantic lookups:

```
lithos_search(query="inbox processing pipeline", mode="hybrid", limit=5)
```

Four modes:

| Mode | When to use |
|------|-------------|
| `hybrid` (default) | Best general-purpose — merges BM25 + cosine similarity via RRF |
| `fulltext` | Exact keyword matching or Tantivy query syntax |
| `semantic` | Meaning matters more than exact words |
| `graph` | Discover related docs by following wiki-links |

**Important**: `lithos_search` does NOT enforce LCMA access scopes and does NOT track retrieval for salience scoring. Use only for exploration.

---

## `lithos_retrieve` — "Give me the best knowledge for this task"

Use for **comprehensive retrieval during actual work** — when quality matters:

```
lithos_retrieve(query="inbox error handling patterns", task_id="...", limit=10)
```

- Runs 10 scouts across multiple backends with Terrace 1 reranking
- Enforces access scopes (`agent_private`, `task` visibility)
- Writes audit receipts — enables cited/misleading feedback loops
- `task_id` activates the task_context scout (extra retrieval dimension)
- **Requires LCMA enabled** — falls back to `lithos_search` if LCMA is off

### Scout Weights (for tuning queries)

**Phase A (parallel):**
- `lexical` (0.22) — BM25 full-text via Tantivy (highest weight)
- `vector` (0.21) — semantic similarity via ChromaDB
- `exact_alias` (0.10) — title/alias exact match
- `graph` (0.13) — wiki-link traversal from Phase A seeds
- `coactivation` (0.10) — docs co-retrieved with Phase A results
- `tags_recency` (0.07) — only fires if tags or path_prefix provided
- `source_url` (0.05) — docs sharing source URLs with Phase A results
- `freshness` (0.04) — only fires if query contains "update", "refresh", "recheck", "verify", or "latest"
- `task_context` (0.04) — only fires if `task_id` provided
- `provenance` (0.04) — follows `derived_from_ids` chains

### Practical Retrieval Tips
- Include `task_id` when working within a task
- Use "latest" or "update" in queries when you want fresh content
- Write good titles — they affect exact_alias matching
- Use `[[wiki-links]]` in content — they feed the graph scout
- Cite useful nodes on task completion — coactivation edges strengthen over time

---

## `lithos_list` with `content_query` — "Filter + search together"

Use when searching within a constrained subset:

```
lithos_list(
    content_query="error handling",
    tags=["project:influx"],
    path_prefix="projects/influx/"
)
```

- `tags`, `author`, `path_prefix` are pushed down into the Tantivy query
- `since`, `title_contains` applied as post-filters
- More efficient than `lithos_search` + manual filtering for constrained queries

---

## `lithos_related` — "What's connected to this doc?"

```
lithos_related(id="doc-uuid", include=["links", "provenance", "edges"])
```

Returns three relationship types:
- **links**: wiki-link graph traversal (BFS depth 1–3)
- **provenance**: `derived_from_ids` chains
- **edges**: typed LCMA edges (flat)
- Plus `related_ids` — deduped union of all referenced IDs
