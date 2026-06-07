# Error Handling Reference

## Knowledge Tool Errors

| Status | Meaning | Action |
|--------|---------|--------|
| `duplicate` | Same `source_url` on another doc | Read `duplicate_of.id`, update it or skip |
| `slug_collision` | Title slugifies to same filename | Use `existing_id` to update, or pick different title |
| `path_collision` | Explicit `.md` path already taken | Use `existing_id` or change path |
| `version_conflict` | Optimistic lock failed | Re-read, get `current_version`, retry |
| `content_too_large` | Content exceeds 1MB | Trim or split the document |
| `invalid_input` | Field validation failed | Check error message for specifics |
| `doc_not_found` | ID doesn't exist | Verify the UUID |
| `search_backend_error` | Tantivy/ChromaDB down | Retry or fall back to a different search mode |
| `lcma_disabled` | LCMA not enabled | Fall back to `lithos_search` |

---

## Collision Types — Know the Difference

Three distinct collision types with different handling:

| Status | Cause | What to Do |
|--------|-------|------------|
| `duplicate` | Same `source_url` exists on another doc | Read existing doc (`duplicate_of.id`), update or skip |
| `slug_collision` | Title slugifies to same filename as existing doc | Use `existing_id` to update that doc, or pick a different title |
| `path_collision` | Explicit `.md` path already taken by a different doc | Same — use `existing_id` or change path |

URL deduplication normalizes before comparison: lowercase scheme/host, strips fragments + tracking params (utm_*, fbclid), sorts query params, removes trailing slashes and default ports.

---

## Stale Cache Pattern

```
result = lithos_cache_lookup(source_url="https://...", max_age_hours=168)

if result["hit"]:
    # Fresh knowledge exists — read and use it
    doc = lithos_read(id=result["document"]["id"])

elif result["stale_exists"]:
    # Expired knowledge exists — update it rather than create a duplicate
    lithos_write(id=result["stale_id"], content="...", agent="<id>")

else:
    # Clean miss — create new
    lithos_write(title="...", content="...", agent="<id>")
```

---

## Optimistic Concurrency

Use `expected_version` on updates when concurrent edits are possible:

```
lithos_write(
    id="<uuid>",
    content="...",
    agent="<id>",
    expected_version=3   # reject if doc has moved past version 3
)
```

On `version_conflict`, the response includes `current_version` — re-read the doc and retry.

---

## Task Tool Responses

| Response | Meaning |
|----------|---------|
| `{success: true}` | Operation succeeded |
| `{success: false}` (on claim) | Another agent holds the claim — not an error |
| `{success: false}` (on release/renew) | Claim expired or not owned |
| `{status: "error", code: "task_not_found"}` | Task doesn't exist or is already closed |

---

## Reserved Metadata Keys

Free-form metadata keys must NOT collide with reserved frontmatter fields. Colliding keys return `invalid_input`:

```
id, title, author, created_at, updated_at, tags, aliases, confidence,
contributors, source, source_url, supersedes, derived_from_ids,
expires_at, version, schema_version, namespace, access_scope,
note_type, status, summaries, entities
```

---

## Update Field Semantics

These are inconsistent across fields — know them:

| Field | Omit/null | Empty value | Non-empty |
|-------|-----------|-------------|-----------|
| `tags` | Preserve existing | `[]` clears all | Replaces entirely |
| `source_url` | Preserve existing | `""` clears | Sets new value |
| `derived_from_ids` | Preserve existing | `[]` clears | Replaces entirely |
| `expires_at` | Preserve existing | `""` clears | Sets new value |
| `confidence` | Preserve existing | — | Sets new value |
| `metadata` | Preserve existing | `{}` is a no-op | Per-key merge (null value deletes key) |

---

## Observability

```
lithos_stats()                    # KB health: doc counts, open tasks, index drift
lithos_node_stats(node_id="...")  # Per-doc: salience, retrieval_count, cited/misleading counts
```

A document with ≥3 `misleading` marks is auto-quarantined and excluded from all future search and retrieve results.
