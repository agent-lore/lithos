# Unified Write Contract

This document is the canonical write contract for Lithos across:

- `lithos_write` (single-item write)
- `lithos_write_batch` (bulk-write v3 item payloads and per-item outcomes)

If another plan conflicts with this document, this document wins.

System-level rollout and compatibility constraints are defined in `final-architecture-guardrails.md`.

## Goals

1. One payload model for single and batch writes.
2. One response envelope for create/update/duplicate outcomes.
3. One manager-layer invariant path (no server-only enforcement).
4. Clear omit-vs-clear behavior on update.

## Canonical Write Fields

Required create fields:

- `title: str`
- `content: str`
- `agent: str`

Required update fields:

- `id: str` (UUID)
- `agent: str`

Optional shared fields (create/update as applicable):

- Core: `tags`, `confidence`, `path`, `source_task`
- Free-form metadata: `metadata` (key/value dict persisted to frontmatter via `KnowledgeMetadata.extra`)
- Provenance and dedup: `source_url`, `derived_from_ids`
- Freshness: `ttl_hours`, `expires_at`
- LCMA metadata: `note_type`, `namespace`, `access_scope`, `entities`, `status`, `schema_version`
- Concurrency controls (batch and future single-write parity): `if_match_updated_at`, `if_match_hash`
- Idempotency (batch): `idempotency_key`

Mapping rule:

- `source_task` is stored in metadata field `source` (task-oriented provenance).
- `source_url` is stored separately in metadata field `source_url` (URL provenance and dedup key).

## Update Semantics (Omit vs Clear vs Replace)

Update requests must distinguish omitted values from explicit clears.

`source_url`:

- omitted: preserve existing value
- `null`: clear existing value
- `str`: normalize and set (dedup-checked)

`derived_from_ids`:

- omitted: preserve existing value
- `[]`: clear existing value
- non-empty list: replace full set (after normalization/validation)

`ttl_hours` and `expires_at`:

- if both provided: reject (`invalid_input`)
- `ttl_hours`: compute absolute `expires_at` from current UTC time
- `expires_at`: parse and store as UTC instant
- omitted: preserve existing `expires_at` on update
- explicit `expires_at: null`: clear expiry

`metadata` (free-form key/value, #305):

- omitted / `null`: preserve existing metadata
- `{}`: clear all metadata
- non-empty dict: additive per-key merge into existing metadata. A key whose
  value is `null` deletes that key; other keys are set; absent keys are
  preserved. This mirrors task metadata merge semantics (shared
  `lithos._merge.merge_metadata`).

Other optional fields follow standard patch semantics:

- omitted: preserve
- provided value: replace

## Validation and Invariants

All validation and invariants are manager-owned and shared by single and batch writes.

### Source URL

- Only `http`/`https`.
- Empty/whitespace is invalid.
- Normalize before persistence and dedup checks:
  - lowercase scheme/host
  - remove fragment
  - remove default ports
  - normalize trailing slash
  - sort query params
  - drop tracking params (`utm_*`, `fbclid`)
- Dedup invariant: one normalized `source_url` maps to at most one document ID.

### Derived Provenance

- `derived_from_ids` must be UUIDs.
- Normalize by trim + dedup + sort.
- Reject self-reference on update (`id` in `derived_from_ids`).
- Missing source IDs are non-fatal warnings, not hard errors.

Authority rule:

- Canonical declared lineage is frontmatter `derived_from_ids`.
- Any graph/edges representation is a projection/cache.

### Freshness

- `expires_at` stored in frontmatter as optional datetime.
- Staleness logic uses `expires_at` and optional read-time age cutoffs.

### LCMA Metadata

- Enums must be validated (`note_type`, `access_scope`, `status`).
- Defaults applied when absent.
- Before LCMA metadata support ships, these fields may be rejected as `invalid_input`/`unsupported_feature`.

### Free-form Metadata (#305)

- `metadata` must be an object with string keys; non-dict input or non-string
  keys are rejected as `invalid_input`.
- Keys must not collide with reserved frontmatter fields
  (`KnowledgeMetadata` known keys such as `title`, `tags`, `version`,
  `source_url`, …). Colliding keys would be silently dropped by frontmatter
  serialization, so they are rejected as `invalid_input` instead.
- Persisted into `KnowledgeMetadata.extra`, which serializes as **top-level**
  frontmatter keys (Obsidian-compatible) and round-trips on read.
- `expected_version` optimistic locking applies to metadata writes unchanged.

## Read and List Return Shapes

- `lithos_read` returns the free-form metadata as an isolated dict under
  `metadata.extra`, alongside the full frontmatter `metadata` envelope.
- `lithos_list` includes the free-form metadata dict on each item under the
  `metadata` key.
- Both `lithos_list` and `lithos_task_list` accept `metadata_match` (#306) to
  filter by free-form metadata: AND across keys, where each `key: q` matches a
  record whose stored value equals `q` or is a list containing `q`. Query values
  are scalars; matching is type-sensitive. `lithos_list` is backed by an
  in-memory inverted index, so a metadata-filtered list never scans the whole
  knowledge base. `lithos_task_list` pushes the predicate into SQLite
  (`json_extract`/`json_each`), so it is engine-evaluated (no Python post-scan)
  and composes with indexed filters such as `status`; a metadata-only query can
  still scan the `tasks` table until an expression index on the queried key is
  added (a documented future optimization).

## Canonical Write Outcome Envelope

`lithos_write` and batch per-item outcomes must use:

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

Notes:

- `warnings` is always present (possibly empty) **on success and actionable
  outcomes**; error envelopes (below) never carry `warnings`.
- Typical warning: unresolved `derived_from_ids` references.
- `duplicate` is specific to source URL dedup policy.
- `duplicate` is not an internal server error; it is a first-class write outcome.
- The full set of first-class (top-level `status`) outcomes is `created` /
  `updated` / `duplicate` / `slug_collision` / `path_collision` /
  `version_conflict` (which carries `current_version`).

Batch-mode policy:

- `best_effort`: `duplicate` is recorded per-item and processing continues.
- `all_or_nothing`: any non-apply outcome (including `duplicate`) aborts publish of staged writes.

## Error Model

Errors are machine-readable and consistent across single and batch write paths.
Every error uses the canonical envelope built by `lithos.envelopes`:

```python
{"status": "error", "code": "<stable_snake_case>", "message": "<sentence>"}
```

Validation failures carry the reserved code `invalid_input`. Error envelopes
never include `warnings`.

Core codes:

- `invalid_input`
- `invalid_uuid`
- `unsupported_feature`
- `path_collision`
- `stale_write_conflict`
- `doc_not_found`
- `internal_error`

Notes:

- Source URL dedup collisions are represented as write outcome `status="duplicate"` in the canonical envelope.

Batch-only projection/workflow codes may add:

- `index_backend_unavailable`
- `graph_update_failed`
- `projection_retry_exhausted`

## Single vs Batch Consistency Rules

1. Same validation logic and manager invariants.
2. Same field semantics.
3. Same status envelope for per-item write results.
4. Batch status/reporting adds workflow state only; it does not redefine write semantics.

## MCP Boundary Semantics

Omit-vs-clear behavior is normative at the manager layer:

- field omitted (sentinel `_UNSET`) -> preserve existing value on update
- field set to `None` -> clear (for fields that support clear)
- field set to a value -> normalize and apply

FastMCP limitation: FastMCP uses Pydantic `TypeAdapter` on plain function signatures, which does not track `model_fields_set`. Both "field omitted from JSON" and "field present with `null`" deliver `None` to the Python function. The MCP tool boundary therefore cannot distinguish omit from null natively.

Convention at the MCP tool boundary:

- field omitted or `null` in request JSON -> preserve existing value on update
- field set to `""` (empty string) -> clear (for clearable string fields: `source_url`)
- field set to a value -> normalize and apply

Implementation requirement:

- Manager layer must have conformance tests proving `_UNSET`, `None`, and value are distinguishable for `source_url`, `expires_at`, and `derived_from_ids`.
- Tool layer must have conformance tests proving omit (preserve), `""` (clear), and value (set) work correctly for `source_url`.

## Index and Migration Rules

Frontmatter-only additions usually require no content migration.

If search backend schema shape changes (e.g., Tantivy fields), startup must:

1. detect incompatibility
2. recreate index as needed
3. trigger full rebuild in the same boot

## Observability Requirement

Write and batch paths are instrumented through OTEL foundation (`telemetry.py`, `traced`, `lithos_metrics`), not separate telemetry systems.

## API Ergonomics Follow-up

The pre-1.0 interface exposes many optional write parameters. Two options were considered:

1. **Grouped request objects** at the MCP boundary (`provenance`, `freshness`, `lcma`).
2. **Grouped documentation only** — keep the flat parameter surface, but introduce section headers in the tool docstring and spec tables mirroring the same taxonomy.

Phase 8 is scoped to **option 2**. Rationale:

- The primary caller is an MCP agent reading a flat tool schema. Nested object params tend to raise argument-construction errors in tool-use rather than reduce them.
- The flat shape does not prevent any invariant that the canonical field contract (this document) already enforces in the manager layer — grouping would be purely cosmetic.
- A breaking change to every caller and every conformance test is disproportionate to the ergonomic delta.

The canonical taxonomy used in docs and the `lithos_write` docstring is:

- **Core (required):** `title`, `content`, `agent`
- **Identity & metadata:** `id`, `tags`, `metadata`, `confidence`, `path`
- **Provenance:** `source_url`, `derived_from_ids`, `source_task`
- **Freshness:** `ttl_hours`, `expires_at`
- **Concurrency:** `expected_version`, and the batch-only `if_match_updated_at` / `if_match_hash`
- **LCMA:** `schema_version`, `namespace`, `access_scope`, `note_type`, `status`, `summaries`, `entities`

Grouped request objects remain a possible post-1.0 addition, introduced **additively** (accepted alongside the flat params) rather than as a replacement, and only if pressure from human-facing MCP client authors materialises.
