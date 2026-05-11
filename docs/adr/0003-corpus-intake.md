---
status: accepted
---

# Corpus mutations flow through a CorpusIntake module

`lithos_write` and `lithos_delete` both ran the same five-step Corpus-mutation pipeline inline at the tool handler: ensure-agent → call into `KnowledgeManager` → synchronise the Search engine → synchronise the link graph → emit the `NOTE_*` event. The pipeline wasn't named anywhere; each handler restated it. The ordering invariants (event-after-indices, no lock outside `KnowledgeManager._write_lock`, search-failure leaves the doc on disk and skips the event) were unwritten contracts. A new corpus-mutating caller — bulk import, file-watcher repair path — would have to know all of them to behave correctly. Apply the deletion test: the seam concentrates ~80 lines of orchestration that would otherwise be re-stated in every caller. We're extracting the pipeline into a `CorpusIntake` Module in `src/lithos/intake.py`; both handlers funnel through it. Migration is staged: delete first (this ADR), write second.

## Considered Options

- **Status quo (inline at every handler).** Rejected — the deletion test fails: removing the seam re-spreads the same five steps with the same ordering rules across every caller, free to drift.
- **Owned by `KnowledgeManager` (mirroring ADR-0001).** Rejected — would force the corpus manager to depend on `SearchEngine`, `KnowledgeGraph`, `CoordinationService`, and `EventBus`. Reconcile is corpus-driven (the corpus tells views what to repair); intake is the inverse — the writer drives both. Putting both directions on `KnowledgeManager` dilutes its identity as the manager of the Corpus.
- **Unified `apply(mutation)` method.** Rejected — write and delete have different return shapes, different `KnowledgeManager` entry points, different view calls. A single dispatch method becomes a type-check on every line. Two methods is the honest shape.
- **Peer of `KnowledgeManager` on `LithosServer`, two methods (`write` and `delete`).** Accepted.

## Consequences

- `CorpusIntake` lives at `src/lithos/intake.py`. It takes `(knowledge, search, graph, coordination, event_bus)` at construction, holds no state, acquires no lock — `KnowledgeManager._write_lock` remains the single serialisation point for Corpus writes.
- `LithosServer` constructs the intake in `initialize()` (after `SearchEngine.create()` returns), mirroring the late-binding pattern already used for `self.search`.
- The `lithos_delete` handler becomes a one-call wrapper: build a `DeleteRequest`, call `intake.delete(agent, request)`, shape the `DeleteOutcome` into the MCP envelope.
- Migration mirrors ADR-0001's staged shape: delete first; write follows in a separate change. Intermediate state has `lithos_delete` flowing through intake while `lithos_write` still runs inline. Both paths still emit the same events, hit the same views, and pass the same tests.
- Search-engine and link-graph exceptions during intake **propagate** to the caller; the document is already off disk and **no event is emitted**. The corpus is the source of truth and a failed view sync is exactly the **Drift** condition that **Reconcile** repairs (ADR-0001). A cleaner "doc-written-but-drifting" outcome that returns success with a warning was considered and deferred — it changes the agent-visible contract and deserves its own ADR.
- Event-emit failures stay non-propagating: a dropped event must never undo a successful corpus write. `LithosServer._emit` is replicated as a private method on `CorpusIntake` so the intake doesn't depend on the server class.
- The file-watcher path (`server.py:handle_file_change`, `handle_file_rename`) runs a similar but distinct pipeline. It is **not** routed through intake in this change. Folding it in is plausible future work. **Amended per ADR-0007:** an earlier version of this bullet claimed *"emit-before-delete is load-bearing for the file-watcher case and would need to be expressed at the seam, not papered over."* Subsequent inspection of `EventBus.emit` (queue-based dispatch via `asyncio.Queue.put_nowait`, no synchronous subscriber path) and the two extant subscribers (`EnrichWorker`, the SSE stream) showed that no subscriber can observe pre-delete `_meta_cache` state regardless of emit ordering — `EnrichWorker._resolve_node_id` deliberately reads `payload["id"]` only for `NOTE_DELETED`. The real invariant on the watcher delete is narrower: **capture-before-mutate** (path→id resolution must precede `KnowledgeManager.delete` because `delete` clears `_id_to_path`). ADR-0007 extracts the watcher pipeline into a `WatchIntake` Module — peer of `CorpusIntake` — with the corrected invariant expressed at the seam.
