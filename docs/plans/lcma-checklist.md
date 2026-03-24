# LCMA Implementation Checklist

This checklist tracks implementation progress for Phase 7 (LCMA Rollout).
Design reference: `lcma-design.md`

Dependencies: Phases 0 through 6.5 complete ✅

Exit criteria (all MVPs):
- LCMA features remain additive and consistent with canonical write contract
- On-disk compatibility preserved throughout rollout
- All existing 24 tools preserved with no renames or removals

---

## Prerequisites (before MVP 1 implementation)

- [ ] Wrap `full_text_search()`, `semantic_search()`, and `hybrid_search()` calls in `asyncio.to_thread()` at call sites — sync search must not block the event loop when LCMA adds 7+ parallel scouts (see design doc §5.13)
- [ ] Implement `merge_and_normalize()` with per-scout min-max normalization to `[0, 1]` — all `lithos_retrieve` scores must be normalized (see design doc §5.3.1); `lithos_search` scores are unchanged
- [ ] Add `EDGE_UPSERTED = "edge.upserted"` event type to `events.py` — all state changes must flow through the event bus (see design doc §3.1 runtime architecture)

---

## MVP 1 — Core Infrastructure

### New frontmatter fields (optional, backward-compatible defaults)

- [ ] Add `schema_version` (int, default 1) — lazy default at read time, persisted on next `lithos_write`
- [ ] Add `namespace` (str, derived from path if absent) — lazy default at read time, persisted on next `lithos_write`; writable via `lithos_write` for explicit overrides
- [ ] Add `access_scope` (enum: `shared|task|agent_private`, default `shared`) — advisory visibility to reduce noise, not a security control; agents self-identify via `agent_id`; project-level scoping handled by `namespace`
- [ ] Add `note_type` (enum: `observation|agent_finding|summary|concept|task_record|hypothesis`, default `observation`)
- [ ] Add `entities` field (list of extracted entity names) — **deferred to MVP 2**: auto-extracted by `lithos-enrich`; not populated in MVP 1
- [ ] Add `status` (enum: `active|archived|quarantined`, default `active`)
- [ ] Add `summaries` nested object (`short`, `long`) (optional)
  - MVP 1: agent-written only (via `lithos_write`); MVP 2: `lithos-enrich` may auto-generate for notes where `summaries` is empty and `note_type` is `concept`/`summary` — agent-written values take precedence
- [ ] Extend `lithos_write` with optional LCMA params while preserving shared write contract and status envelope
- [ ] Define preserve/set/clear semantics for each new `lithos_write` LCMA field before implementation
- [ ] Implement lazy defaults + write-back-on-touch for LCMA fields (no migration runner in MVP 1)

### Storage
- [ ] Create `data/.lithos/edges.db` with `edges` table and indexes
- [ ] Create `data/.lithos/stats.db` with `node_stats`, `coactivation`, `enrich_queue`, and `working_memory` tables (`enrich_queue`: `id`, `trigger_type`, `node_id`, `task_id`, `triggered_at`, `processed_at`; index on `processed_at`; `working_memory`: `task_id`, `node_id`, `activation_count`, `first_seen_at`, `last_seen_at`, `last_receipt_id`; PK `(task_id, node_id)`; index on `task_id`)
- [ ] Add `receipts` table to `stats.db` (schema: `id` TEXT PK, `ts`, `query`, `namespace_filter`, `agent_id`, `task_id`, `temperature`, `scouts_fired`, `candidates_considered`, `terrace_reached`, `final_nodes`, `conflicts_surfaced`; indexes on `ts`, `task_id`, `agent_id`)
- [ ] Add `LithosConfig.lcma` configuration subtree (`LcmaConfig` schema in design doc §7.z): `enabled`, `enrich_drain_interval_minutes`, `rerank_weights`, `note_type_priors`, `temperature_default`, `temperature_edge_threshold`, `wm_eviction_days`, `llm_provider`

### Retrieval
- [ ] Add `lithos_retrieve` tool orchestrating scouts internally
- [ ] Implement vector scout (wraps existing `ChromaIndex.search()`)
- [ ] Implement lexical scout (wraps existing `TantivyIndex.search()`)
- [ ] Implement exact/alias/path scout (wraps existing `KnowledgeGraph` link resolution: `_alias_to_node`, slugified title, UUID prefix, path/filename matching)
- [ ] Implement tags/recency scout (wraps existing `KnowledgeManager.list_all()`)
- [ ] Implement provenance scout (walk `derived_from_ids` forward and reverse via existing provenance index; sequential, needs Phase A seeds)
- [ ] Implement task-context scout (notes linked by same `task_id` via findings/claims in `coordination.db`; only activated when `task_id` provided)
- [ ] Implement freshness scout (stale-but-relevant notes via existing `expires_at`/`is_stale`; activates more strongly on update/refresh/recheck query signals)
- [ ] Implement Terrace 1 fast re-rank with `note_type` priors (all neutral 0.5 in MVP 1), diversity (MMR), and basic salience
- [ ] Define `note_type_prior()` lookup table: all types = 0.5 in MVP 1 (configurable via `LcmaConfig.note_type_priors`; differentiated priors deferred to MVP 2+)
- [ ] All scouts apply `namespace_filter` and `access_scope` gating before returning candidates
- [ ] MVP 1 explicitly keeps legacy tools (`lithos_read`, `lithos_search`, `lithos_list`) backward-compatible; caller-context-aware scope enforcement begins in `lithos_retrieve`
- [ ] `scout_contradictions` is a no-op stub in MVP 1 (returns empty list); activated in MVP 2
- [ ] `lithos_retrieve` accepts optional `surface_conflicts` boolean (default `False`) — when True, surfaces contradiction edges in results
- [ ] `lithos_retrieve` response shape compatible with `lithos_search`: top-level `results` key, per-item `score` always a normalized float (matching hybrid-mode `SearchResult`); per-item fields `id`, `title`, `snippet`, `score`, `path`, `source_url`, `updated_at`, `is_stale`, `derived_from_ids` preserved; LCMA-only extras `reasons`, `scouts`, `salience` additive; envelope adds `temperature`, `terrace_reached`, `receipt_id`
- [ ] `lithos_retrieve` upserts into `working_memory` per `(task_id, node_id)` when `task_id` is provided — increments `activation_count`, updates `last_seen_at`, sets `last_receipt_id` (task-shared WM only; per-agent WM deferred)

### Learning
- [ ] Basic positive reinforcement on retrieval (salience + spaced rep strength updates in `stats.db`)
- [ ] Coactivation count updates in `stats.db`
- [ ] Make internal `_reconcile_provenance_projection` work with `data/.lithos/edges.db` (remove `supported=False` guard, implement actual repair logic; reconcile stays internal, not an MCP tool)

### New tools

- [ ] `lithos_edge_upsert` — create or update a typed edge in `edges.db`
- [ ] `lithos_edge_list` — query edges by node, type, or namespace

**Exit criteria:**

- `lithos_retrieve` returns ranked results using 7 scouts (vector, lexical, exact/alias, tags/recency, provenance, task-context, freshness), with response shape compatible with `lithos_search`
- Retrieval receipts written to `receipts` table in `stats.db`
- `edges.db` and `stats.db` created and populated on first use
- Existing notes without LCMA fields remain fully readable (defaults applied at read time)

---

## MVP 2 — Reinforcement & Namespacing

- [ ] Finalize `lithos-enrich` pseudocode (§5.12 in design doc) before implementation begins

### Background process
- [ ] Introduce `lithos-enrich` as an in-process background worker with two triggering modes:
  - **Incremental**: subscribe to the existing in-memory Lithos event bus (queue-based `subscribe()` API with event types `note.created`, `note.updated`, `note.deleted`, `task.completed`, `finding.posted`, `edge.upserted`); write to `enrich_queue`; periodic drain (e.g. every 5 min) processes pending entries, deduplicating node-level work by `node_id` and task-level work by `task_id` (see design doc §4.4)
  - **Full sweep**: daily scheduled run (configurable interval) across all nodes — recomputes decay, catches anything missed by incremental runs (concept cluster analysis deferred to MVP 3)
- [ ] Treat the daily full sweep as authoritative repair for any missed best-effort incremental triggers
- [ ] WM eviction in daily full sweep: evict entries where task is completed/cancelled or `last_seen_at` exceeds `wm_eviction_days` TTL (default: 7 days)

### Reinforcement

- [ ] Negative reinforcement: penalize ignored nodes (salience decay in `stats.db` when chronically ignored)
- [ ] Negative reinforcement: penalize misleading nodes with stronger salience decay + quarantine threshold
- [ ] Weaken edges that pulled in bad-context nodes

### Contradiction workflow

- [ ] Contradiction edges: `type="contradicts"` with `conflict_state` in `edges.db`
- [ ] `lithos_conflict_resolve` tool (resolution states: `unreviewed|accepted_dual|superseded|refuted|merged`)
- [ ] Contradiction surfacing in retrieval when `surface_conflicts=True` is passed to `lithos_retrieve`

### New tools and extensions

- [ ] `lithos_node_stats` — view salience and usage stats from `stats.db`
- [ ] Extend `lithos_task_complete` with optional feedback params: `cited_nodes: list[str]`, `misleading_nodes: list[str]` — server calls `post_task_update()` on receipt

### Other

- [ ] Namespace + `access_scope` filtering applied in all scouts
- [ ] Consolidation in `lithos-enrich` triggered via `enrich_queue` (`task.completed` entries from `lithos_task_complete` events; also runs during daily full sweep for all tasks since last run)
- [ ] Graph scout querying both NetworkX wiki-link graph and `edges.db` typed edges
- [ ] Coactivation/bridge scout: find nodes that frequently co-occur or connect separate clusters via `coactivation` table in `stats.db`
- [ ] Source-url/domain scout: notes from the same normalized URL family or host via existing `_source_url_to_id` map; activated when query or seed nodes have `source_url` set
- [ ] `lithos-enrich` auto-extracts `entities` from notes (deferred from MVP 1)
- [ ] Schema migration registry (`data/.lithos/migrations/registry.json`) — deferred from MVP 1; needed only for semantic schema changes
- [ ] Implement schema migration runner (idempotent, never removes existing fields)
- [ ] Differentiated `note_type_priors` tuning based on MVP 1 learning data

**Exit criteria:**

- Retrieval utility improves over time via positive/negative reinforcement
- Contradictions are surfaced and resolvable
- Namespace isolation works across agents

---

## MVP 3 — Advanced Cognition

- [ ] Analogy scout: frame extraction (`{problem, constraints, actions, outcome, lessons}`) + structural matching
- [ ] Computed temperature in `lithos_retrieve` Terrace 1: `temperature = 1 - coherence` (coherence = mean edge strength among top candidates) — activated when edges exceed threshold; MVP 1 returns fixed default (0.5)
- [ ] Temperature-guided exploration depth (high temp → deeper exploration, more `scout_exploration` weight)
- [ ] `scout_exploration` with novelty/random/mixed modes
- [ ] Concept nodes: regular notes with `note_type: "concept"` created via `lithos_write` — concepts are derived clusters first, with explicit promotion/materialization (not auto-materialized early)
- [ ] Concept node formation from stable coactivation clusters (`maybe_update_concepts`) — runs inside `lithos-enrich`, not `lithos_retrieve` hot path
- [ ] Concept node damping: salience ceiling + diversity penalty for repeated concept retrieval
- [ ] Embedding space versioning via separate ChromaDB collections per space (`knowledge_<space_id>`)
- [ ] Multi-space vector scout during embedding migration
- [ ] `lithos_receipts` tool — query retrieval audit history from `receipts` table in `stats.db`
- [ ] Background LLM synthesis in `lithos-enrich` (requires `LithosConfig.lcma.llm_provider` config) — produces persistent artifacts (summaries, concept notes, edge annotations), not query-time reranking

**Exit criteria:**

- Analogy scout returns structurally similar notes across domains
- Temperature operationalized and controlling exploration depth
- Concept nodes emerge from usage patterns without manual curation
- Embedding model can be upgraded without losing retrieval quality during transition
