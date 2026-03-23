# LCMA Implementation Checklist

This checklist tracks implementation progress for Phase 7 (LCMA Rollout).
Design reference: `lcma-design.md`

Dependencies: Phases 0 through 6.5 complete ✅

Exit criteria (all MVPs):
- LCMA features remain additive and consistent with canonical write contract
- On-disk compatibility preserved throughout rollout
- All existing 24 tools preserved with no renames or removals

---

## MVP 1 — Core Infrastructure

### New frontmatter fields (optional, backward-compatible defaults)
- [ ] Add `schema_version` (int, default 1)
- [ ] Add `namespace` (str, derived from path if absent)
- [ ] Add `access_scope` (enum: `agent_private|task|project|shared|user_private`, default `shared`) — advisory filtering to reduce noise, not a security control; agents self-identify via `agent_id`
- [ ] Add `note_type` (enum: `observation|agent_finding|summary|concept|task_record|hypothesis`, default `observation`)
- [ ] Add `entities` (list of extracted entity names)
- [ ] Add `status` (enum: `active|archived|quarantined`, default `active`)
- [ ] Add `summaries` nested object (`short`, `long`) (optional)
- [ ] Extend `lithos_write` with optional LCMA params while preserving shared write contract and status envelope
- [ ] Define preserve/set/clear semantics for each new `lithos_write` LCMA field before implementation

### Storage
- [ ] Create `data/.lithos/edges.db` with `edges` table and indexes
- [ ] Create `data/.lithos/stats.db` with `node_stats`, `coactivation`, `enrich_queue`, and `working_memory` tables (`enrich_queue`: `id`, `trigger_type`, `node_id`, `task_id`, `triggered_at`, `processed_at`; index on `processed_at`; `working_memory`: `task_id`, `node_id`, `activation_count`, `first_seen_at`, `last_seen_at`, `last_receipt_id`; PK `(task_id, node_id)`; index on `task_id`)
- [ ] Create `data/.lithos/receipts.jsonl` append-only audit log (each entry includes `id` field for `receipt_id` reference)
- [ ] Create `data/.lithos/migrations/registry.json` schema migration registry
- [ ] Implement schema migration runner (idempotent, never removes existing fields)
- [ ] Add `LithosConfig.lcma` configuration subtree for enrich cadence, Terrace 2 provider settings, and LCMA feature flags

### Retrieval
- [ ] Add `lithos_retrieve` tool orchestrating scouts internally
- [ ] Implement vector scout (wraps existing `ChromaIndex.search()`)
- [ ] Implement lexical scout (wraps existing `TantivyIndex.search()`)
- [ ] Implement tags/recency scout (wraps existing `KnowledgeManager.list_all()`)
- [ ] Implement Terrace 1 fast re-rank with `note_type` priors, diversity (MMR), and basic salience
- [ ] All scouts apply `namespace_filter` and `access_scope` gating before returning candidates
- [ ] MVP 1 explicitly keeps legacy tools (`lithos_read`, `lithos_search`, `lithos_list`) backward-compatible; caller-context-aware scope enforcement begins in `lithos_retrieve`
- [ ] `scout_contradictions` is a no-op stub in MVP 1 (returns empty list); activated in MVP 2
- [ ] `lithos_retrieve` accepts optional `query_class` parameter (enum: `lookup|debug|design|planning|write|synthesis|decision`, default `lookup`)
- [ ] `lithos_retrieve` response shape compatible with `lithos_search`: top-level `results` key, per-item `score` always a normalized float (matching hybrid-mode `SearchResult`); per-item fields `id`, `title`, `snippet`, `score`, `path`, `source_url`, `updated_at`, `is_stale`, `derived_from_ids` preserved; LCMA-only extras `reasons`, `scouts`, `salience` additive; envelope adds `temperature`, `terrace_reached`, `receipt_id`, `query_class_used`

### Learning
- [ ] Basic positive reinforcement on retrieval (salience + spaced rep strength updates in `stats.db`)
- [ ] Coactivation count updates in `stats.db`
- [ ] Make internal `_reconcile_provenance_projection` work with `edges.db` (remove `supported=False` guard, implement actual repair logic; reconcile stays internal, not an MCP tool)

### New tools
- [ ] `lithos_edge_create` — create/update a typed edge in `edges.db`
- [ ] `lithos_edge_list` — query edges by node, type, or namespace
- [ ] `lithos_node_stats` — view salience and usage stats from `stats.db`

Exit criteria:
- `lithos_retrieve` returns ranked results using vector + lexical + tags/recency scouts, with response shape compatible with `lithos_search`
- Retrieval receipts written to `receipts.jsonl`
- `edges.db` and `stats.db` created and populated on first use
- Existing notes without LCMA fields remain fully readable (defaults applied at read time)

---

## MVP 2 — Reinforcement & Namespacing

### Background process
- [ ] Introduce `lithos-enrich` as an in-process background worker with two triggering modes:
  - **Incremental**: subscribe to the existing in-memory Lithos event bus from within the server process; `lithos_write`, `lithos_delete`, `lithos_task_complete`, `lithos_finding_post`, `lithos_edge_create/update` events write to `enrich_queue`; periodic drain (e.g. every 5 min) processes pending entries, deduplicating by `node_id`
  - **Full sweep**: daily scheduled run (configurable interval) across all nodes — recomputes decay, full concept cluster analysis, catches anything missed by incremental runs
- [ ] Treat the daily full sweep as authoritative repair for any missed best-effort incremental triggers

- [ ] Negative reinforcement: penalize ignored nodes per query class (`stats.db` updates)
- [ ] Negative reinforcement: penalize misleading nodes with stronger salience decay + quarantine threshold
- [ ] Weaken edges that pulled in bad-context nodes
- [ ] Contradiction edges: `type="contradicts"` with `conflict_state` in `edges.db`
- [ ] `lithos_conflict_resolve` tool (resolution states: `unreviewed|accepted_dual|superseded|refuted|merged`)
- [ ] Contradiction surfacing in retrieval for design/decision/synthesis/debug query classes
- [ ] Namespace + `access_scope` filtering applied in all scouts
- [ ] Consolidation in `lithos-enrich` triggered via `enrich_queue` (`task_complete` entries from `lithos_task_complete` events; also runs during daily full sweep for all tasks since last run)
- [ ] Graph scout querying both NetworkX wiki-link graph and `edges.db` typed edges
- [ ] `lithos_edge_update` tool (adjust weight or conflict state)

Exit criteria:
- Retrieval utility improves over time via positive/negative reinforcement
- Contradictions are surfaced and resolvable
- Namespace isolation works across agents

---

## MVP 3 — Advanced Cognition

- [ ] Analogy scout: frame extraction (`{problem, constraints, actions, outcome, lessons}`) + structural matching
- [ ] Temperature computation in `lithos_retrieve` Terrace 1: `temperature = 1 - coherence` (coherence = mean edge strength among top candidates)
- [ ] Temperature-guided exploration depth (high temp → deeper exploration, more `scout_exploration` weight)
- [ ] `scout_exploration` with novelty/random/mixed modes
- [ ] Concept nodes: regular notes with `note_type: "concept"` created via `lithos_write`
- [ ] Concept node formation from stable coactivation clusters (`maybe_update_concepts`) — runs inside `lithos-enrich`, not `lithos_retrieve` hot path
- [ ] Concept node damping: salience ceiling + diversity penalty for repeated concept retrieval
- [ ] Embedding space versioning via separate ChromaDB collections per space (`knowledge_<space_id>`)
- [ ] Multi-space vector scout during embedding migration
- [ ] `lithos_receipts` tool — query retrieval audit history from `receipts.jsonl`
- [ ] Terrace 2 LLM interpretive pass in `lithos-enrich` (requires `LithosConfig.lcma.llm_provider` config) — not in `lithos_retrieve` hot path

Exit criteria:
- Analogy scout returns structurally similar notes across domains
- Temperature operationalized and controlling exploration depth
- Concept nodes emerge from usage patterns without manual curation
- Embedding model can be upgraded without losing retrieval quality during transition
