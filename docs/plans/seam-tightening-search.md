# Seam tightening: SearchEngine + indices reconcile

Implementation plan for ADR 0002 (in full) and ADR 0001 indices slice. Graph and provenance reconcile folds are out of scope here — they land in later ADR-0001 phases.

Each phase is a single coherent change leaving the codebase green against the AGENTS.md done criteria (`make test`, `make test-integration`, `make lint`, `make typecheck`, `make check`).

---

## Phase 1 — Types and async factory

**Add** in `src/lithos/search.py`:

- `IndexableDocument` (frozen dataclass): `id`, `title`, `content`, `tags`, `created_at`, plus whatever fields Tantivy/Chroma actually consume — confirm by reading current `add_document` paths.
- `HealthStatus` — sealed shape: `Healthy | Unhealthy(reason: str)`.
- `ReconcileAction(backend, action, reason)`, `SearchReconcilePlan`, `SearchReconcileResult`, `ReconcileFailure`.
- `SearchEngine.create(config) -> "SearchEngine"` async classmethod. Opens both backends, awaits embedding model load, returns a fully usable engine.

**Don't touch** the existing `__init__` yet — keep it as the internal constructor, called from `create`. Lazy model load is disabled inside `create`.

**Tests:**

- `create()` returns an engine with the model loaded.
- `create()` fails cleanly when the model load fails.
- `create()` re-quarantines a corrupt Chroma store the way the existing init path does (preserve behaviour at `search.py:910-916`).

---

## Phase 2 — Public status surface

**Add** to `SearchEngine`:

- `def health(self) -> HealthStatus` — composes existing `chroma.health_check` plus a Tantivy probe; logs subsystem detail at WARN/ERROR but returns one combined signal.
- `def count_documents(self) -> int` — wraps `tantivy.count_docs()`.
- `def count_chunks(self) -> int` — wraps `chroma.count_chunks()`.

**Tests:**

- Healthy when both backends respond.
- Unhealthy with reason when Chroma is corrupt or model load failed.
- Counts match what the backends report.

No callers migrated yet — old paths still work in parallel.

---

## Phase 3 — `plan_reconcile_to` / `apply_reconcile` on SearchEngine

**Add** to `SearchEngine`:

- `plan_reconcile_to(docs: Iterable[IndexableDocument]) -> SearchReconcilePlan` — internalises the detection at `reconcile.py:130-161` (`needs_rebuild`, `get_indexed_doc_ids`, doc-set diff). Plan stores the docs (per Q8=i in the design grilling).
- `async apply_reconcile(plan: SearchReconcilePlan) -> SearchReconcileResult` — internalises the apply at `reconcile.py:178-196` (`rebuild_from_docs`, `clear` + `add_document` loop), returning per-action status.

**Tests** (these are the tests `_reconcile_indices` should have had):

- Plan returns `noop` when corpus matches indexes.
- Plan returns `full_rebuild / schema_mismatch` when Tantivy reports rebuild needed.
- Plan returns `full_rebuild / doc_set_mismatch` when corpus and index disagree.
- Apply executes the plan idempotently.
- Apply surfaces per-backend failures via `failed: tuple[ReconcileFailure, ...]`.

Still no caller migration. The new methods are unused.

---

## Phase 4 — `KnowledgeManager.plan_reconcile` / `apply_reconcile` (indices-only composition)

**Move** `_scan_corpus` from `reconcile.py:67` onto `KnowledgeManager` as a private async method (KM owns the corpus). Keep the export from `reconcile.py` as a thin shim during the migration so the graph and provenance scopes still work — the shim disappears in their fold-phases later.

**Add** to `KnowledgeManager`:

- `async def plan_reconcile(self) -> ReconcilePlan` — scans corpus, calls `search.plan_reconcile_to(docs)`, wraps in `ReconcilePlan(search=...)`. Single-view today; gains `graph=`, `provenance=` fields in later ADR-0001 phases.
- `async def apply_reconcile(self, plan: ReconcilePlan) -> ReconcileResult` — calls `search.apply_reconcile(plan.search)`, wraps the result.

**Tests:**

- `KM.plan_reconcile()` produces a plan whose search slice matches `SearchEngine.plan_reconcile_to()` when given the same corpus.
- `KM.apply_reconcile()` end-to-end repairs an artificially-drifted corpus.

---

## Phase 5 — Migrate callers off SearchEngine internals

Largest commit by line count. Worth its own PR.

### Production code

| File | Line | Old | New |
| --- | --- | --- | --- |
| `cli.py` | 532, 538 | `engine.tantivy.index`, `engine.chroma.collection.count()` | `engine.health()`, `engine.count_chunks()` |
| `cli.py` | 465 | `lithos reconcile` command | `scope==indices`: dispatch via `KM.plan_reconcile` + `apply_reconcile`. `scope in (graph, provenance_projection)`: keep calling `reconcile.py` unchanged. `scope==all`: KM for indices + `reconcile.py` for the others. |
| `server.py` | 301 | `chroma.health_check` | `engine.health()` |
| `server.py` | 591 | `tantivy.needs_rebuild` | inspect a `KM.plan_reconcile()` result, or remove this surface entirely if it was internal-only |
| `server.py` | 606 | `chroma._model is None` | **delete** — eager `create()` makes this state unreachable |
| `server.py` | 738, 748, 3292 | `tantivy.count_docs()`, `chroma.count_chunks()` | `engine.count_documents()`, `engine.count_chunks()` |

**Construction:** every `SearchEngine(config)` site becomes `await SearchEngine.create(config)`. Audit FastMCP server lifespan, all CLI commands, and every test fixture.

### Test migration

| File | Old | New |
| --- | --- | --- |
| `tests/test_integration_conformance.py:211` | `server.search.chroma.collection.count()` | `server.search.count_chunks()` |
| `tests/test_integration_conformance.py:219` | `server.search.chroma.collection.get(where={"doc_id": ...})` | search for the doc via the public interface, assert no result (per Q10=m) |
| `tests/test_integration_conformance.py:2181` | `monkeypatch.setattr(server.search.tantivy, "count_docs", lambda: stale_count)` | `monkeypatch.setattr(server.search, "count_documents", lambda: stale_count)` |
| `tests/test_cli_contract.py:232-233` | broken-collection structural mock | stub `engine.health()` returning `Unhealthy("...")` |
| `tests/test_server.py:126, 2337, 2409` | direct `.chroma` paths | review case-by-case; same approach |

---

## Phase 6 — Shrink reconcile.py

- Delete `_reconcile_indices` (its logic now lives behind `KM`).
- Update the public `reconcile()` aggregator: when scope is `indices` or `all`, delegate to `KM.plan_reconcile` / `apply_reconcile`; keep `_reconcile_graph` and `_reconcile_provenance_projection` as-is.
- Update `test_reconcile.py` for indices to assert via `KM`; leave the other two scopes alone.
- `_scan_corpus` shim stays until graph + provenance fold (later ADR-0001 phases). Tag the shim with a comment pointing at ADR-0001.

---

## Phase 7 — Privatise the backends

- Rename `SearchEngine.tantivy` → `_tantivy`, `SearchEngine.chroma` → `_chroma`. Drop any `@property` getters that exposed them.
- Run `make check` plus `make test-integration`. Any remaining external reference is a missed migration; fix in place.
- Verify no occurrences of `\.tantivy\b|\.chroma\b` outside `search.py` and `config.py` (which uses the strings only as filesystem paths).

---

## Out of scope (later ADR-0001 phases)

- `KnowledgeGraph.plan_reconcile_to` / `apply_reconcile` plus KM composition for the `graph` scope.
- `ProvenanceProjection.plan_reconcile_to` / `apply_reconcile` plus KM composition for `provenance_projection`.
- Final deletion of `reconcile.py` and removal of the `_scan_corpus` shim.

---

## Risks and callouts

- **Slow startup.** Eager model load adds seconds to `SearchEngine.create()`. Tests should use a lightweight config or stub the embedding-model factory. If startup latency matters operationally, it's an ops concern documented by ADR 0002.
- **Phase 5 is the largest commit.** Touches every `SearchEngine` construction site and every backend reach. Worth its own PR; phases 1–4 can land as a single setup PR.
- **The `_scan_corpus` shim is technical debt with a known expiry.** It leaves with the graph fold; comment-tag it on introduction.
- **Done-criteria gate** (per AGENTS.md): every phase passes `make test`, `make test-integration`, `make lint`, `make typecheck`, `make check` before merging.

---

## Linked decisions

- ADR 0001 — Reconciliation lives on KnowledgeManager, not as a peer module
- ADR 0002 — SearchEngine hides Tantivy and ChromaDB
- CONTEXT.md — Corpus, Indexable document, Drift, Reconcile, Reconcile plan, Search engine
