# Digest Auto-Linking v2 (Provenance-First)

Contract note: `lithos_write` request/response semantics in this plan are governed by `unified-write-contract.md`. System-level rollout and compatibility guardrails are governed by `final-architecture-guardrails.md`.

## Goal

Allow agents writing digest/synthesis notes to declare which notes they are derived from, without requiring manual `[[wiki-links]]` in content.

The core goal is provenance tracking and retrieval, not changing wiki-link graph behavior.

## Design Principles

1. Provenance is first-class data in metadata.
2. The wiki-link graph remains a projection of content links only.
3. Provenance queries use dedicated indexes/APIs, not overloaded graph traversal.
4. Invariants are enforced in the manager layer, not only server tools.

## Proposed Design

### 1. Metadata: `derived_from_ids`

Add `derived_from_ids: list[str] = []` to `KnowledgeMetadata`, persisted in frontmatter.

Rules:
- Values must be UUID strings.
- Normalize by trimming, deduplicating, and sorting.
- `None` is not persisted; stored value is always a list.
- Titles, slugs, and paths are invalid in this field and must be rejected.

Round-trip:
- `KnowledgeMetadata.to_dict` / `from_dict`
- `KnowledgeManager.create` / `update` / `_scan_existing`

### 2. Write API (`lithos_write`)

Add optional param:
- `derived_from_ids: list[str] | None = None`

Semantics:
- Create:
  - omitted or `None` => store `[]`
  - non-empty list => validate/normalize/store
- Update:
  - omitted or `None` => preserve existing value
  - `[]` => clear
  - non-empty list => replace entire set

Validation:
- Reject malformed UUID entries.
- Reject non-UUID identifiers (including titles, slugs, and paths).
- Reject self-reference (`id` contained in `derived_from_ids`) on update.
- Missing source IDs do not fail the write; they produce warnings.

### 3. Manager-Level Provenance Indexes

In `KnowledgeManager`, maintain:
- `_doc_to_sources: dict[str, list[str]]` (doc_id -> derived_from_ids)
- `_source_to_derived: dict[str, set[str]]` (source_id -> docs derived from it)
- `_unresolved_provenance: dict[str, set[str]]` (missing_source_id -> docs referencing it)

Build in `_scan_existing`; maintain on create/update/delete.

These indexes provide efficient provenance traversal and avoid dependence on graph cache state.

### 4. Dedicated Provenance Tool

Add a new MCP tool instead of overloading `lithos_links`:

`lithos_provenance(id: str, direction: "sources" | "derived" | "both" = "both", depth: int = 1, include_unresolved: bool = True)`

Returns:
- `sources`: nodes this document derives from
- `derived`: nodes derived from this document
- optional `unresolved_sources` entries for unknown IDs

Canonical lineage note:

- `lithos_provenance` is the authoritative provenance query interface.
- Any future projected `derived_from` representation in LCMA `edges.db` is an accelerator, not a replacement API.

Depth behavior:
- BFS over provenance indexes only (not wiki-link edges)
- clamp depth to sane bounds (e.g. 1-3)

### 5. Keep Wiki Graph Unchanged (for now)

`KnowledgeGraph` remains responsible for `[[wiki-links]]` only.

No `edge_type` change in NetworkX in this phase.
No conversion to `MultiDiGraph` in this feature.

Rationale:
- avoids edge-type overwrite bugs with `DiGraph`
- avoids ambiguous mixed-edge traversal semantics
- lowers migration risk and testing complexity

### 6. Unified `lithos_write` Response Shape

Use status-based responses consistently with other planned changes:

```python
{"status": "created", "id": "...", "path": "...", "warnings": []}
{"status": "updated", "id": "...", "path": "...", "warnings": []}
```

Warnings include unresolved provenance IDs:

```python
{
  "status": "updated",
  "id": "...",
  "path": "...",
  "warnings": ["derived_from_ids contains missing document: <uuid>"]
}
```

## Behavior Details

### Missing Source Documents

If a referenced source ID is absent:
- write succeeds
- warning is returned
- unresolved reference is tracked in `_unresolved_provenance`

When a source document is later created, the `create()` method in `KnowledgeManager` must check `_unresolved_provenance` for entries referencing the new document's ID. For each referencing document found, the relationship is moved: removed from `_unresolved_provenance` and added to `_source_to_derived`. This check occurs at the end of `create()`, after the new document's own provenance indexes are updated. No frontmatter mutation is required — only in-memory indexes change.

### Deletions

If a source document is deleted:
- do not mutate other documents' frontmatter
- mark affected relationships unresolved in indexes
- provenance queries continue to show lineage with unresolved markers

### Cycles

Provenance cycles are allowed but traversal must track visited nodes to avoid loops.

## Files

| File | Changes |
|------|---------|
| `knowledge.py` | Add `derived_from_ids` to `KnowledgeMetadata`; validation/normalization helpers; manager-level provenance indexes; create/update/delete/scan maintenance; query helpers for provenance traversal |
| `server.py` | Add `derived_from_ids` to `lithos_write`; return warnings; add `lithos_provenance` tool |
| `graph.py` | No functional changes in v2 (wiki-link graph stays unchanged) |
| `docs/SPECIFICATION.md` | Document `lithos_write` new param and status-based returns; add `lithos_provenance` |
| `tests/` | Add metadata round-trip tests; write semantics tests; validation tests; unresolved/resolved transition tests; provenance traversal tests; delete behavior tests |

## Migration and Compatibility

- Backward compatible with existing notes (missing `derived_from_ids` treated as `[]`).
- No graph schema/cache migration required in v2.
- No change to existing wiki-link behavior or `lithos_links` results.

## Scope

Medium. Most work is in metadata handling and manager indexes, with a small server surface for tool wiring.

Risk is significantly lower than mixing provenance into NetworkX edge types in the same change.

## Future (Optional)

If a unified relationship graph is later needed:
1. Introduce `MultiDiGraph` in a dedicated migration.
2. Backfill provenance edges with explicit edge keys/types.
3. Define mixed-edge traversal semantics explicitly.
4. Only then consider adding edge-type filtering to `lithos_links`.
