---
status: accepted
---

# Reconciliation lives on KnowledgeManager, not as a peer module

`reconcile.py` was a peer module orchestrating drift detection and repair across three views — full-text/semantic indexes, link graph, and provenance projection. It owned no state and made no decisions; it scanned the corpus, asked each view what it had, and called rebuild operations on the views' internals (`search.tantivy.rebuild_from_docs(...)`, `search.chroma.clear()`, etc.). Apply the deletion test: removing the module didn't concentrate complexity, it just exposed that the views' rebuild operations were already public-by-leak. We're folding reconciliation onto `KnowledgeManager`, the module that already owns the corpus as source of truth, and giving each downstream view a private plan/apply pair invoked through it. The agent-facing interface is `KnowledgeManager.plan_reconcile()` / `apply_reconcile(plan)`; views never expose rebuild operations publicly.

## Considered Options

- **Peer module (status quo).** Rejected — passes the deletion test as shallow.
- **`SearchEngine.reconcile_from(corpus)` etc., views as peers.** Rejected — pushes corpus shape across every view's seam, and the agent-facing entry point becomes "call N modules in order."
- **`KnowledgeManager.reconcile()` orchestrating views internally.** Accepted.

## Consequences

- `reconcile.py` is deleted at the end of the migration.
- Each view (`SearchEngine`, `KnowledgeGraph`, the LCMA provenance projection) gets its own private `plan_reconcile_to(docs) → Plan` / `apply_reconcile(plan) → Result` pair, exposed only to `KnowledgeManager`.
- Migration is staged: indices first (the work this ADR was opened for), then graph, then provenance projection. The intermediate state has `reconcile.py` shrinking but still alive — accepted, since each step is reversible on its own.
- The CLI command `lithos reconcile` and any MCP tool dispatch through `KnowledgeManager` after the indices step lands; the surface stays the same for callers.
