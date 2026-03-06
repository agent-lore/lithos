# Final Architecture Guardrails

This document defines cross-cutting guardrails for the target Lithos end state.

It complements `unified-write-contract.md`:

- `unified-write-contract.md` defines write request/response semantics.
- this document defines system-level rollout, compatibility, observability, and verification constraints.

If a plan conflicts with these guardrails, these guardrails win.

## 1) Compatibility Policy (Pre-1.0)

Lithos is pre-1.0 and currently single-operator. Therefore:

- MCP/API behavior may change without backward-compat shims when it improves system coherence.
- On-disk compatibility is required: existing markdown/frontmatter data must remain readable and correct.
- Migrations must prioritize preserving existing knowledge content over preserving older tool response shapes.

## 2) Canonical Sources

Normative references:

1. `docs/plans/unified-write-contract.md` (write semantics)
2. `docs/plans/final-architecture-guardrails.md` (system guardrails)
3. `docs/SPECIFICATION.md` (public product specification)

Plan documents should reference these instead of redefining contracts inline.

## 3) Migration Framework and Startup Ordering

All schema/store/index upgrades follow one startup framework:

1. Load core config and telemetry bootstrap.
2. Run store migrations in deterministic order:
   - markdown/frontmatter migrations (if any)
   - sqlite migrations (`coordination.db`, `edges.db`, `stats.db`, `batch.db`)
3. Open search/index backends.
4. If index schema mismatch is detected:
   - recreate affected index
   - trigger full rebuild in same boot before serving ready traffic
5. Mark service ready only after migration/rebuild critical path succeeds.

Failure policy:

- never mutate or delete markdown source-of-truth content during failed migrations
- fail fast on irreversible migration errors
- emit clear actionable logs and OTEL spans for each migration phase

## 4) Projection Consistency Contract

Lithos has two planes:

- write plane (authoritative): markdown/frontmatter
- projection plane (derived): Tantivy, ChromaDB, graph caches, mirrored relationship projections

Client-visible guarantees:

1. Successful write-phase commits are immediately visible in `lithos_read`/`lithos_list`.
2. Search/semantic/graph/projection views may lag.
3. Lag and per-item projection errors are visible via batch status and system diagnostics.

## 5) Reconcile and Repair

Provide an explicit reconcile tool:

`lithos_reconcile(scope: "all" | "indices" | "graph" | "provenance_projection" = "all", dry_run: bool = false) -> { ... }`

Responsibilities:

- rebuild stale/missing indices from markdown source of truth
- repair provenance projection drift (`derived_from_ids` -> projected edges)
- report counts: scanned, repaired, failed, skipped
- support dry-run preview for safe operations

Phasing rule:

- Before provenance projection storage exists (`edges.db` derived projection), `scope="provenance_projection"` must return a deterministic no-op result (`supported: false`, `reason: "not_enabled"`), not an error.

## 6) Observability Baseline

All new subsystems use OTEL foundation (`telemetry.py`, `traced`, `lithos_metrics`).

Minimum required telemetry:

- span coverage for write, batch apply/project/reconcile, migration phases
- operation counters and latency histograms
- structured logs carrying stable identifiers (`doc_id`, `batch_id`, `migration_step`)

Sensitive-data policy:

- do not emit raw note content, raw prompts, or full queries in span attributes by default
- prefer hashed or length-based query attributes when needed

## 6.1 Provenance API Authority

- Canonical lineage queries use `lithos_provenance` (frontmatter/index authority).
- Edge queries (`lithos_edge_list`) are graph/learning oriented and may include projected lineage edges.
- If results diverge, lineage answers follow `lithos_provenance`.

## 7) Conformance Test Matrix

Maintain a single cross-plan conformance suite covering:

1. single write semantics (create/update/duplicate/warnings)
2. batch parity with single-write semantics
3. source URL dedup invariants and concurrency
4. provenance validation and unresolved warning behavior
5. freshness fields and stale lookup behavior
6. schema mismatch recreate + full rebuild path
7. reconcile repair correctness and idempotency
8. event emission only after successful commits, with subscriber failure isolation
9. webhook/SSE delivery semantics (at-least-once delivery, duplicate-safe `event.id`, restart-safe webhook retries)
10. OTEL span/metric emission for critical paths

No major feature should be considered complete without passing this suite.

## 8) Security and Scope Defaults

Until explicit auth exists, enforce conservative defaults:

- default `access_scope` remains `shared` unless caller provides narrower scope
- namespace and scope filters are always applied in retrieval/ranking paths
- telemetry redaction is on by default
- external event delivery surfaces inherit the same auth boundary as MCP when auth exists

Note: `access_scope` is a retrieval scoping mechanism to avoid search pollution across agents and namespaces, not a security boundary. All agents operate in the same trust domain per `SPECIFICATION.md` section 1.2 (Non-Goals).

## 9) Target Search Schema Registry

Maintain one explicit target search schema registry in docs (`target-search-schema.md`), and update it whenever schema-affecting fields are added.

Upgrade rule:

- batch schema-breaking index changes when practical to avoid repeated full rebuilds across adjacent releases.
