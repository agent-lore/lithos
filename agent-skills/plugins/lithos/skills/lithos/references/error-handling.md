# Error Handling Reference

## The Canonical Error Envelope

Every tool failure is one shape — check `status`, then branch on `code`:

```
{"status": "error", "code": "<stable_snake_case>", "message": "<sentence>"}
```

`code` is machine-stable; never parse `message`. Validation failures carry
`code: "invalid_input"` — including unparseable datetime filters (`since`,
`active_since`). Error envelopes never include `warnings`. Protocol-level
errors (MCP `ToolError`) occur only when a request is rejected by schema
validation before the handler runs, or on unexpected internal exceptions.

Common codes (illustrative, not exhaustive — any tool may return other codes,
always in the same shape; task-graph tools also emit `invalid_edge_type`,
`self_edge`, `cycle`, `not_a_gate`, `parent_exists`, `invalid_relation_type`,
`invalid_task_type` — see `task-graph.md`):

| Code | Meaning | Action |
|------|---------|--------|
| `invalid_input` | Field validation failed | Check error message for specifics |
| `invalid_metadata_key` | Task metadata contains `depends_on`/`blocked_on`, or note metadata collides with a reserved key | Use the `depends_on` param or task edges; rename the note key |
| `content_too_large` | Content exceeds the size limit | Trim or split the document |
| `doc_not_found` | ID doesn't exist | Verify the UUID |
| `note_not_found` | `lithos_note_update` id doesn't exist | Verify the UUID |
| `task_not_found` | Task doesn't exist or is closed | Verify the task id |
| `task_not_resolved` | `lithos_task_reopen` on a task that is already open | Nothing to do |
| `claim_failed` | Claim denied — aspect held by another agent, or task closed/missing | Normal contention; try another task or wait |
| `claim_not_found` | Renew/release with no matching active claim | Claim expired or not yours; re-claim if still needed |
| `search_backend_error` | Tantivy/ChromaDB down | Retry or fall back to a different search mode |
| `lcma_disabled` | LCMA not enabled | Fall back to `lithos_search` |
| `internal_error` | Unexpected write failure | Retry; report if persistent |

## Write Outcomes (not errors)

Actionable write results keep their own top-level `status` — they carry
payloads you act on:

| Status | Meaning | Action |
|--------|---------|--------|
| `duplicate` | Same `source_url` on another doc | Read `duplicate_of.id`, update it or skip |
| `slug_collision` | Title slugifies to same filename | Use `existing_id` to update, or pick different title |
| `path_collision` | Explicit `.md` path already taken | Use `existing_id` or change path |
| `version_conflict` | Optimistic lock failed | Re-read, get `current_version`, retry |

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
| `{status: "error", code: "claim_failed"}` (on claim) | Another agent holds the aspect, or the task is closed/missing — normal contention, not a fault |
| `{status: "error", code: "claim_not_found"}` (on release/renew) | Claim expired or not owned |
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
| `metadata` (`lithos_write`) | Preserve existing | `{}` clears ALL free-form metadata | Per-key merge (null value deletes key) |
| `metadata` (`lithos_note_update`, `lithos_task_update`) | Preserve existing | `{}` is a no-op | Per-key merge (null value deletes key) |

---

## Observability

```
lithos_stats()                    # KB health: doc counts, open tasks, index drift
lithos_node_stats(node_id="...")  # Per-doc: salience, retrieval_count, cited/misleading counts
```

A document with ≥3 `misleading` marks is auto-quarantined and excluded from all future search and retrieve results.
