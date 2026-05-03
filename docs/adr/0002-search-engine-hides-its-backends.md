---
status: accepted
---

# SearchEngine hides Tantivy and ChromaDB

Callers reached past `SearchEngine` to `.tantivy` and `.chroma` for rebuild, count, health, and "is the embedding model loaded yet?" — six call sites across `cli.py`, `reconcile.py`, `server.py`, and tests. The two backends were de facto part of the interface. We're tightening the seam: `TantivyIndex` and `ChromaIndex` become private; `SearchEngine` exposes a single agent-facing health signal (`Healthy | Unhealthy(reason)`), public `count_documents()` / `count_chunks()` for status surfaces, and an internal plan/apply pair for reconciliation. The embedding model is loaded eagerly at construction via `await SearchEngine.create(config)` so no caller ever observes a half-initialised engine. Documents cross the seam as a small `IndexableDocument` value, not the full `KnowledgeDocument`.

## Considered Options

- **Status quo.** Rejected — six external call sites already prove the seam isn't real, and one of them reaches into a private attribute (`chroma._model is None`).
- **Public per-backend operations on `SearchEngine`** (`rebuild_tantivy()`, `clear_chroma()`, etc.). Rejected — preserves the leak in renamed form; backends are still part of the interface.
- **Single agent-facing health** + **private backends** + **private reconcile plan/apply**. Accepted.

## Consequences

- `SearchEngine.tantivy` and `SearchEngine.chroma` become private. External callers must use the public surface; tests that asserted on backend internals (e.g. `chroma.collection.get(where={"doc_id": ...})`) shift to public-interface assertions (search for the doc, assert no result).
- Subsystem-specific health diagnostics ("Tantivy is degraded but Chroma is fine") are no longer exposed to agents — those are operator concerns and live in logs and telemetry.
- Construction of `SearchEngine` is async-only via `create(config)`. The model load happens once, up front, and may take seconds on first run; the cost is paid at startup rather than on first query.
- A future need to swap a backend (different vector store, alternative full-text engine) becomes a single-module change instead of an audit of every caller — the second adapter that proves the seam is real.
