# Lithos

Local, privacy-first MCP server providing a shared knowledge base for AI agents. The corpus on disk is the source of truth; every other store (full-text index, semantic index, link graph, provenance projection) is a derivable view that must agree with it.

## Language

**Corpus**:
The set of Markdown notes on disk under the configured data directory. Treated as the single source of truth.
_Avoid_: store, dataset, library.

**Indexable document**:
The slice of a note the search seam consumes — id, title, content, tags, created_at, and the few other fields the indexes actually use. Distinct from `KnowledgeDocument`, which is the manager-facing type with frontmatter, metadata, and lifecycle.
_Avoid_: search doc, indexed doc.

**Drift**:
The condition where a derived view (search indexes, link graph, provenance projection) disagrees with the corpus. Not a bug in any one component — an integrity property between corpus and views.
_Avoid_: stale index, sync gap, inconsistency.

**Reconcile**:
The operation that detects drift and brings a derived view back into agreement with the corpus. Always corpus-driven: the corpus is never modified by a reconcile. May be planned (compute drift, return a description) and applied (execute the plan) as separate phases.
_Avoid_: rebuild, resync, repair.

**Reconcile plan**:
A description of what reconciliation would do — per-view actions and reasons (e.g. "rebuild the full-text index because schema_mismatch"). Carries enough context that applying it is mechanical. The plan **is** the dry-run output.
_Avoid_: diff, repair list.

**Search engine**:
The module owning every full-text and semantic search concern. Holds the Tantivy and Chroma indexes internally; their existence is not part of its interface. Health is one signal — agents either get a healthy engine or an unhealthy one with a reason; subsystem-level diagnostics are operator concerns.
_Avoid_: search service, indexer.

## Relationships

- The **Corpus** is the source of truth. The **Search engine**, the link graph, and the provenance projection are derived views.
- A view is in **Drift** when its contents disagree with the **Corpus**.
- A **Reconcile** consumes the **Corpus**, produces a **Reconcile plan** describing the **Drift**, then applies it to bring the view into agreement.
- The **Search engine** consumes **Indexable documents**, never `KnowledgeDocument` directly.

## Example dialogue

> **Dev:** "After deleting a note, the search results still mention it. Is that a search bug?"
> **Maintainer:** "Probably not — the **Corpus** lost the note but the **Search engine** has **Drifted**. A **Reconcile** against indices would detect the doc-set mismatch and rebuild."
> **Dev:** "Should I expose the rebuild action?"
> **Maintainer:** "No — agents call `reconcile`. They don't pick which view to rebuild. That's an operator concern."

## Flagged ambiguities

- "rebuild" was used in the code to mean both "full re-index a single backend" (Tantivy operation) and "the whole reconciliation flow" — resolved: the operator-facing operation is **Reconcile**; rebuilding a backend is an internal action inside an apply step.
