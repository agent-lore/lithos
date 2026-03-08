# Implementation Checklist (Preferred Order)

This checklist defines the preferred execution order across the active plans:

- `otel-plan.md`
- `source_url-dedup.md`
- `event-bus-plan.md`
- `digest-auto-link-v2.md`
- `research-cache-plan.md`
- `bulk-write-v3.md`
- `reconcile-plan.md`
- `event-delivery-plan.md`
- `lcma-design.md`
- `cli-extension-plan.md`

Normative references:

- `unified-write-contract.md`
- `final-architecture-guardrails.md`

---

## Phase 0 - Canonical Contracts and Guardrails

- [x] Create canonical write contract doc (`unified-write-contract.md`)
- [x] Create system guardrails doc (`final-architecture-guardrails.md`)
- [x] Define target search schema registry doc (Tantivy + semantic metadata fields)
- [x] Update active plans to reference canonical docs
- [x] Update `docs/SPECIFICATION.md` with pre-1.0 compatibility policy and status envelope

Exit criteria:

- No active plan defines a conflicting write contract
- API change policy is explicit: MCP can change, on-disk compatibility is required
- Target index schema is documented in one place before schema-affecting implementation starts

---

## Phase 1 - Observability Foundation (OTEL)

- [x] Add optional OTEL dependencies and config
- [x] Implement `telemetry.py` no-op + active modes
- [x] Wire setup/shutdown in CLI lifecycle
- [x] Instrument `knowledge.py`, `search.py`, `coordination.py`, and tool handlers
- [x] Add telemetry tests and lifecycle test

Dependencies:

- Phase 0 complete

Exit criteria:

- Core write/search/coordination paths emit spans and baseline metrics
- Telemetry disabled mode has no functional behavior changes

---

## Phase 2a - Source URL Dedup + Provenance Surface

- [x] Add `source_url` metadata field and normalization
- [x] Add manager-owned dedup map and write-time invariants
- [x] Add create/update duplicate behavior using status envelope
- [x] Index `source_url` in Tantivy and Chroma metadata
- [x] Add startup duplicate audit and deterministic logging
- [x] Implement index schema mismatch recreate + full rebuild path
- [x] Return `source_url` in read/search/semantic/list responses

Dependencies:

- Phase 0 complete
- Phase 1 recommended (for migration/rebuild observability)

Exit criteria:

- Duplicate URL writes are consistently blocked by manager invariants
- Existing on-disk notes remain readable after upgrade
- Rebuild path succeeds automatically on schema mismatch

---

## Phase 2b - Internal Event Bus

- [x] Add `events.py` with `EventBus` class, `LithosEvent` dataclass, and in-memory ring buffer
- [x] Add `EventsConfig` to `config.py` (enabled flag, buffer size)
- [x] Wire `EventBus` into `LithosServer` lifecycle
- [x] Emit events from write/delete/task/finding/agent-register success paths in `server.py`
- [x] Emit events from file watcher `handle_file_change`

Dependencies:

- Phase 0 complete
- Phase 1 recommended (for event-path observability)

Exit criteria:

- Internal event bus emits on all write/task success paths (no delivery surface yet)

---

## Phase 3 - Digest Provenance v2 (`derived_from_ids`)

- [ ] Add `derived_from_ids` frontmatter field + validation/normalization
- [ ] Add manager provenance indexes (`_doc_to_sources`, `_source_to_derived`, `_unresolved_provenance`)
- [ ] Implement write semantics (`None` preserve, `[]` clear, list replace)
- [ ] Add warnings for unresolved source IDs
- [ ] Add `lithos_provenance` tool (BFS on provenance indexes only)
- [ ] Keep wiki-link graph behavior unchanged

Dependencies:

- Phase 0 complete
- Phase 2a recommended (shared provenance fields already surfaced)

Exit criteria:

- Declared lineage is canonical in frontmatter
- Missing lineage references are non-fatal and queryable

---

## Phase 4 - Research Cache and Freshness

- [ ] Add `expires_at` metadata + staleness helper
- [ ] Extend `lithos_write` with `ttl_hours`/`expires_at` per unified contract
- [ ] Add `lithos_cache_lookup`
- [ ] Add freshness fields (`updated_at`, `is_stale`) to search/semantic responses
- [ ] Align search schema changes with rebuild framework using the single batched Tantivy schema jump decided in `target-search-schema.md`

Dependencies:

- Phase 0 complete
- Phase 2a/3 recommended (provenance + dedup stability improves cache quality)

Exit criteria:

- One-call cache lookup reliably returns hit/miss/stale guidance
- Agents can update stale docs instead of creating duplicates

---

## Phase 5 - Bulk Write v3 (Durable Workflow)

- [ ] Implement batch journal schema and worker lifecycle
- [ ] Add `lithos_write_batch` and `lithos_batch_status` (and optional `lithos_batch_list`)
- [ ] Enforce shared single/batch write contract and status envelope per item
- [ ] Implement best-effort and all-or-nothing apply behavior
- [ ] Add projection retries/dead-letter behavior
- [ ] Emit internal batch lifecycle events (`batch.queued`/`batch.applying`/`batch.projecting`/`batch.completed`/`batch.failed`) from durable status transitions
- [ ] Emit OTEL batch metrics/spans using telemetry foundation

Dependencies:

- Phase 0 complete
- Phase 1 complete
- Phase 2a and 3 complete (shared invariants are reused by batch)
- Phase 2b recommended (batch lifecycle events can emit immediately)
- Phase 4 recommended (freshness fields in unified payload)

Exit criteria:

- Batch and single writes have consistent semantics
- Durable status and retry behavior are operationally visible

---

## Phase 6 - Reconcile/Repair Tooling

- [ ] Complete dedicated implementation plan (`reconcile-plan.md`) review before coding starts
- [ ] Implement `lithos_reconcile` core scopes (`indices`/`graph`/`all`)
- [ ] Implement deterministic no-op behavior for `provenance_projection` when projection store is not enabled
- [ ] Add dry-run mode and repair reporting
- [ ] Add idempotency and crash-safe tests

Dependencies:

- Phases 2a through 5 complete

Exit criteria:

- System can repair projection drift without touching authoritative markdown content

---

## Phase 6.5 - Event Delivery Surface

- [ ] Add SSE endpoint (`GET /events`) with type/tag filtering and replay-from-ID
- [ ] Add webhook registry, durable outbox, and delivery history tables to `coordination.db`
- [ ] Implement webhook dispatcher with HMAC signing, retries, duplicate-safe `event.id` payloads, and restart-safe outbox processing
- [ ] Make SSE inherit the MCP auth boundary when auth exists
- [ ] Add MCP tools: `lithos_webhook_register`, `lithos_webhook_list`, `lithos_webhook_delete`, `lithos_webhook_deliveries`
- [ ] Add event delivery tests (SSE integration, webhook delivery)

Dependencies:

- Phase 2b internal event bus complete
- Phase 6 recommended (reconcile may emit events)

Exit criteria:

- Agents can subscribe to real-time events via SSE or webhooks
- Webhook delivery survives server restart via SQLite-backed outbox

---

## Phase 7 - LCMA Rollout (MVPs in Order)

### MVP 1

- [ ] Add LCMA optional metadata fields (`note_type`, `namespace`, `access_scope`, etc.)
- [ ] Add `lithos_retrieve` with initial scouts + Terrace 1 rerank
- [ ] Add receipts logging
- [ ] Add `edges.db` (`related_to`) and `stats.db` base tables
- [ ] Enable `lithos_reconcile(scope="provenance_projection")` real repair path once projection store exists
- [ ] Add edge/stats tools

### MVP 2

- [ ] Add negative reinforcement and contradiction workflow
- [ ] Add namespace/scope filtering in scouts
- [ ] Add WM to LTM consolidation hook on task completion
- [ ] Add graph scout over NetworkX + edges.db

### MVP 3

- [ ] Add analogy scout and temperature-guided exploration
- [ ] Add concept node formation + damping
- [ ] Add embedding-space versioning and transition querying

Dependencies:

- Phases 0 through 6.5 complete

Exit criteria:

- LCMA features remain additive and consistent with canonical write contract
- On-disk compatibility preserved throughout rollout

---

## Phase 8 - API Ergonomics Cleanup

- [ ] Replace the flat `lithos_write` option surface with grouped request objects (`provenance`, `freshness`, `lcma`) at the MCP boundary
- [ ] Preserve the canonical on-disk semantics and outcome envelope from `unified-write-contract.md`
- [ ] Add compatibility notes in `docs/SPECIFICATION.md` for the cleaned-up pre-1.0 interface
- [ ] Extend single-write and batch conformance coverage to grouped input objects

Dependencies:

- Phase 7 MVP 1 complete (LCMA write fields are present and stable enough to group)

Exit criteria:

- Single and batch write APIs are materially easier to use without changing manager-layer semantics
- Grouped request objects have conformance tests proving parity with canonical field semantics

---

## Phase 9 - Deferred Integration: CLI Extension

- [ ] Revisit `cli-extension-plan.md` after the write and retrieval surfaces stabilize
- [ ] Prioritize CLI phases 1-3 first (JSON output, read/list, CRUD), then graph/coordination/polish

Dependencies:

- Phases 0 through 8 complete

Exit criteria:

- CLI surfaces are built on top of the stabilized core contracts

---

## Cross-Phase Conformance (Run Continuously)

- [ ] Maintain one conformance suite across single write, batch write, dedup, provenance, freshness, migration/rebuild, reconcile, event emission/delivery, and OTEL instrumentation
- [ ] Block milestone completion if conformance suite regresses
