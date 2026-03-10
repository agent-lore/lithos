# Reconcile and Repair Plan

Contract note: reconcile behavior is governed by `final-architecture-guardrails.md`. This document turns those guardrails into an implementable plan.

## Goal

Provide a deterministic repair path for drift in projection systems without mutating markdown source-of-truth content.

The reconcile internals are the repair mechanism for:

- rebuilding stale or missing search indices
- repairing graph cache drift
- repairing projected provenance edges once that projection store exists

Reconcile is not a migration framework and it is not a general-purpose content fixer. It only repairs derived state from authoritative markdown/frontmatter data.

Exposure model:

- implement reconcile in core logic so startup repair paths, CLI/admin commands, and future internal callers share one repair path
- do not expose reconcile as an MCP tool in Phase 6
- expose reconcile via CLI/admin path for operators
- keep system health checks CLI/admin-only as well unless a later requirement justifies an MCP diagnostic surface

## Internal Contract

```python
reconcile(
    scope: Literal["all", "indices", "graph", "provenance_projection"] = "all",
    dry_run: bool = False,
) -> {
    "scope": str,
    "dry_run": bool,
    "supported": bool,
    "status": "ok" | "noop" | "partial_failure" | "failed",
    "summary": {
        "scanned": int,
        "repaired": int,
        "failed": int,
        "skipped": int,
    },
    "actions": list[dict],
    "failures": list[dict],
}
```

Response rules:

- `supported: false` is only used for `scope="provenance_projection"` before the projection store exists.
- `status="noop"` is used when no changes are required or when `dry_run` finds zero pending repairs.
- `actions` contains machine-readable planned or applied repairs.
- `failures` contains stable error codes and affected identifiers.

## Repair Authority

Authority is one-way:

1. Markdown/frontmatter is the source of truth.
2. Search indices, graph caches, and projected provenance edges are derived.
3. Reconcile may delete and rebuild derived state.
4. Reconcile must never rewrite note content or frontmatter to make projections match.

This rule keeps reconcile safe under partial corruption or failed projection updates.

## Scope Behavior

## 1. `scope="indices"`

Repairs Tantivy and ChromaDB from the markdown corpus.

Detection:

- missing index directory or collection
- document count mismatch against current markdown corpus
- missing indexed document for a known note ID
- optionally stale metadata mismatch for target search schema fields

Repair strategy:

- if backend-level schema mismatch or corruption is detected, recreate backend and run full rebuild
- otherwise re-project only missing or stale documents when detection is precise enough
- if precise incremental repair cannot be proven safe, fall back to full rebuild

Dry-run output examples:

- `{"backend": "tantivy", "action": "full_rebuild", "reason": "schema_mismatch"}`
- `{"backend": "chroma", "action": "reindex_doc", "doc_id": "..."}`

## 2. `scope="graph"`

Repairs the wiki-link graph cache and persisted graph artifacts from markdown.

Detection:

- missing graph cache on disk
- note present in corpus but absent from graph
- link-set mismatch between parsed markdown links and cached graph edges

Repair strategy:

- rebuild the graph cache from a fresh markdown scan
- write the graph cache atomically after rebuild succeeds

The graph scope should prefer full rebuild over in-place mutation because the graph is small enough and rebuilds are easier to reason about.

## 3. `scope="provenance_projection"`

Repairs projected lineage edges derived from `derived_from_ids`.

Before LCMA projection storage exists:

- return `supported: false`
- return `status: "noop"`
- return `summary` with zero counts
- include `reason: "not_enabled"` in `actions` or a top-level failure-free note

After projected provenance storage exists in `edges.db`:

- scan all documents with `derived_from_ids`
- compute the expected projection edge set
- compare expected edges with the current projected edge set
- add missing edges and remove stale projected edges

Authority rule:

- projected provenance is always downstream of frontmatter and `lithos_provenance`
- if projected provenance diverges, reconcile makes projection match frontmatter, never the reverse

## 4. `scope="all"`

Runs `indices`, `graph`, then `provenance_projection` in that order.

Ordering rule:

- indices first, because search projections are the most visible operator-facing repair
- graph second, because it is independent of provenance projection storage
- provenance projection last, because it may be unsupported pre-LCMA and should not block the earlier repairs

The aggregated result combines counts and preserves per-scope actions and failures.

## Drift Detection Strategy

Reconcile needs deterministic inputs, not heuristics.

Canonical scan input:

- use `KnowledgeManager.list_all()` or equivalent authoritative markdown inventory
- use normal document parsing/normalization path so manually edited notes are interpreted the same way as normal reads

Detection keys:

- `doc_id`
- `path`
- normalized metadata fields required by the target search schema
- parsed wiki-links
- normalized `derived_from_ids`

Implementation rule:

- all comparison logic should operate on normalized in-memory representations, not raw YAML strings

## Dry-Run Design

`dry_run=True` computes the exact same scan and diff as a real reconcile, but it does not mutate any derived stores.

Requirements:

- no writes to Tantivy, ChromaDB, graph cache files, or SQLite projection stores
- action list must match what a real run would attempt
- counts in `summary` reflect planned actions, not applied actions

This makes dry-run suitable for operator review and CLI/admin diagnostics.

## Idempotency and Crash Safety

Idempotency requirements:

- running reconcile twice in a row with no source changes must produce `status="noop"` on the second run
- partial repair of one backend must not cause duplicate actions on a clean rerun
- adding the same projected provenance edge twice must be prevented by store constraints or upsert logic

Crash-safety requirements:

- use per-scope execution boundaries
- only replace persisted graph cache after successful rebuild
- only mark a scope repair as complete after backend writes succeed
- if `scope="all"` crashes mid-run, a rerun must safely resume by recomputing diffs from source of truth

Recommended implementation pattern:

1. scan source of truth
2. compute expected derived state
3. diff expected vs actual
4. apply repair atomically per backend where possible
5. emit result summary

No repair step should depend on in-memory assumptions from a previous failed run.

## Batch Write Coordination

Batch write coordination is deferred with Phase 5. Phase 6 repair internals should avoid baking in an external `deferred_due_to_active_batch` surface now; add that coordination check when a manual reconcile entry point and active batch apply workflow both exist.

## Observability

Reconcile uses the OTEL foundation.

Required traces:

- `lithos.reconcile`
- `lithos.reconcile.scan`
- `lithos.reconcile.diff`
- `lithos.reconcile.apply`

Required attributes:

- `lithos.reconcile.scope`
- `lithos.reconcile.backend`
- `lithos.reconcile.status`

Required logs/metrics:

- scanned/repaired/failed/skipped counts per scope
- stable identifiers for affected `doc_id`, backend, and error code

Sensitive-data rule:

- never emit note content in spans or logs

## Error Model

Use stable machine-readable codes in `failures`, for example:

- `index_rebuild_failed`
- `graph_rebuild_failed`
- `projection_store_unavailable`
- `projection_diff_failed`
- `repair_apply_failed`
- `internal_error`

Failure handling rules:

- one backend failure should not silently erase successful actions in another backend
- `scope="all"` may return `partial_failure` when at least one scope succeeds and at least one fails
- `dry_run` failures are still real failures if scan/diff could not be completed

## Implementation Sketch

Files likely touched:

| File | Change |
|------|--------|
| `src/lithos/reconcile.py` or equivalent service module | Add reconcile scan/diff/apply orchestration and response shaping |
| `src/lithos/cli.py` | Add operator/admin reconcile command(s) that call core reconcile logic |
| `src/lithos/knowledge.py` | Reuse authoritative document scan/list helpers |
| `src/lithos/search.py` | Add explicit repair/rebuild helpers for Tantivy and ChromaDB |
| `src/lithos/graph.py` | Add graph rebuild/diff helpers and atomic cache write path |
| `src/lithos/telemetry.py` | Add reconcile traces/metrics helpers if needed |
| `tests/test_reconcile.py` | Add dry-run, idempotency, crash-safety, and per-scope repair tests |

If provenance projection lives in LCMA storage later:

| File | Change |
|------|--------|
| `src/lithos/edges.py` or LCMA storage module | Add projected provenance diff/apply helpers |

## Test Plan

Minimum required tests:

1. indices dry-run reports a planned full rebuild without mutating indices
2. indices real run rebuilds and makes search results match markdown corpus
3. graph dry-run reports graph repair actions without touching cache files
4. graph real run rebuilds broken cache and is idempotent on rerun
5. `provenance_projection` returns deterministic no-op before projection store exists
6. projected provenance repair adds missing edges and removes stale ones after store exists
7. `scope="all"` aggregates mixed success into `partial_failure` correctly
8. crash during one scope does not corrupt source-of-truth content and rerun succeeds

Exit criteria:

- each scope has deterministic dry-run and real-run behavior
- reruns are idempotent
- markdown/frontmatter content is never mutated by reconcile
- no mutating MCP repair tool is required for Phase 6
