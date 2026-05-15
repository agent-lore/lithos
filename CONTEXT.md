# Lithos

Local, privacy-first MCP server providing a shared knowledge base for AI agents. The corpus on disk is the source of truth; every other store (full-text index, semantic index, link graph, provenance projection) is a derivable view that must agree with it.

## Language

**Corpus**:
The set of Markdown notes on disk under the configured data directory, **and** the agent-asserted edges in `data/.lithos/edges.db`. Both are agent-authored persistent state and the joint source of truth. Derived views (Search engine, link graph, Provenance projection) are derivable from the notes tier; asserted edges have no derived view. The projection-vs-asserted split inside `edges.db` is enforced by `provenance_type` predicate-scoping (see ADR-0004 and ADR-0006).
_Avoid_: store, dataset, library.

**Indexable document**:
The slice of a note the search seam consumes — id, title, content, tags, created_at, and the few other fields the indexes actually use. Distinct from `KnowledgeDocument`, which is the manager-facing type with frontmatter, metadata, and lifecycle.
_Avoid_: search doc, indexed doc.

**Drift**:
The condition where a derived view (search indexes, link graph, provenance projection) disagrees with the **notes-tier** corpus. Not a bug in any one component — an integrity property between the notes-tier corpus and its views. The asserted-edge tier has no parallel store and so has no drift condition.
_Avoid_: stale index, sync gap, inconsistency.

**Reconcile**:
The operation that detects drift and brings a derived view back into agreement with the corpus. Always corpus-driven: the corpus is never modified by a reconcile. May be planned (compute drift, return a description) and applied (execute the plan) as separate phases. Asserted-edge rows in `edges.db` are not touched by reconcile; the projection-vs-asserted predicate (ADR-0004) ensures plan/apply only writes projection-typed rows. Reconcile remains corpus-driven and never modifies the corpus, in both tiers.
_Avoid_: rebuild, resync, repair.

**Reconcile plan**:
A description of what reconciliation would do — per-view actions and reasons (e.g. "rebuild the full-text index because schema_mismatch"). Carries enough context that applying it is mechanical. The plan **is** the dry-run output.
_Avoid_: diff, repair list.

**Search engine**:
The module owning every full-text and semantic search concern. Holds the Tantivy and Chroma indexes internally; their existence is not part of its interface. Health is one signal — agents either get a healthy engine or an unhealthy one with a reason; subsystem-level diagnostics are operator concerns.
_Avoid_: search service, indexer.

**Provenance projection**:
The derived view of the **Corpus** that holds typed edges projected from note metadata. Today that means frontmatter-declared `derived_from` lineage; the projection may grow later, but the current implementation does not yet mirror wiki-links or `source_url` rows into `edges.db`. The projection owns the existence and all columns of its rows; nothing outside the projection writes to those rows. Agent-asserted edges share the underlying `edges.db` storage but carry a different `provenance_type` and are scoped out of **Reconcile** by predicate. The projection is the third derived view alongside the **Search engine** and the link graph.
_Avoid_: edge store, link projection, edges database.

**Corpus intake**:
The controlled entry point for **Corpus** mutations from agent tools. Ensures agent registration, runs the mutation through the corpus manager (which provides atomicity, including `expected_version` checks), synchronises derived views (Search engine, then link graph), and emits the matching `NOTE_*` event. Distinct from **Reconcile**: intake is agent-driven and updates views as a write happens; Reconcile is corpus-driven and brings views back into agreement after **Drift**.
_Avoid_: ingestion, writer, pipeline, mutator.

**Cognitive memory**:
The agent-facing module that owns retrieval, learning, and the agent-asserted derived state. Built on top of the **Search engine**, the link graph, and the **Provenance projection**. Internally hosts the scouts, the parallel-terraced-scan retrieve orchestrator, the stats store (salience, retrieval counts, receipts, coactivation, working memory), reinforcement, and the in-process enrichment worker. Distinct from the **Provenance projection**, which is corpus-derived state with a drift condition; cognitive memory is accumulated agent state with no drift-from-corpus condition. The acronym _LCMA_ (Lithos Cognitive Memory Architecture) is acceptable in code and file paths, but design conversations use "cognitive memory".
_Avoid_: LCMA (in design conversations), retrieval engine, working memory (which is a sub-concern, not the module).

## Relationships

- The **Corpus** is the source of truth. The **Search engine**, the link graph, and the **Provenance projection** are derived views.
- A view is in **Drift** when its contents disagree with the **Corpus**.
- A **Reconcile** consumes the **Corpus**, produces a **Reconcile plan** describing the **Drift**, then applies it to bring the view into agreement. Each derived view exposes a private plan/apply pair to its owner (`KnowledgeManager`).
- A **Corpus intake** is the inverse direction: an agent-driven mutation flows from the tool surface, through the corpus, out to every derived view, and onto the event bus.
- The **Search engine** consumes **Indexable documents**, never `KnowledgeDocument` directly.
- **Cognitive memory** depends on the **Search engine**, the link graph, and the **Provenance projection** (it reads through their public surfaces) and is the only agent-facing module for retrieval and learning. Its accumulated state has no **Drift** condition and is therefore not subject to **Reconcile**.

## Example dialogue

> **Dev:** "After deleting a note, the search results still mention it. Is that a search bug?"
> **Maintainer:** "Probably not — the **Corpus** lost the note but the **Search engine** has **Drifted**. A **Reconcile** against indices would detect the doc-set mismatch and rebuild."
> **Dev:** "Should I expose the rebuild action?"
> **Maintainer:** "No — agents call `reconcile`. They don't pick which view to rebuild. That's an operator concern."

## Flagged ambiguities

- "rebuild" was used in the code to mean both "full re-index a single backend" (Tantivy operation) and "the whole reconciliation flow" — resolved: the operator-facing operation is **Reconcile**; rebuilding a backend is an internal action inside an apply step.
