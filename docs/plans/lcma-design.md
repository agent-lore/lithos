# LCMA Design Doc

Contract note: write-path request/response semantics referenced here are governed by `unified-write-contract.md`. System-level rollout and compatibility guardrails are governed by `final-architecture-guardrails.md`.

## 0. Summary

### Intellectual lineage

LCMA's retrieval architecture is inspired by Douglas Hofstadter's **Parallel Terraced Scan (PTS)**, the search strategy at the heart of his Copycat and Metacat models of analogy-making. In PTS, many lightweight “codelets” (here called **scouts**) probe the problem space in parallel at low cost. A global **temperature** signal — derived from how coherent or fragmented the current candidates are — controls whether the system exploits familiar, well-connected knowledge or explores novel, loosely related material. When temperature is high (low coherence among results), exploration dominates; when temperature is low (strong agreement), the system narrows and deepens.

This explains several LCMA design choices that might otherwise seem unusual:

- **Scouts** are deliberately cheap and overlapping — redundancy is a feature, not waste, because each scout captures a different retrieval signal (lexical, semantic, graph, recency, provenance).
- **Terraces** are levels of increasing computational cost. Terrace 0 is the parallel scout union; Terrace 1 applies fast reranking. The system stops as soon as the result quality is sufficient, rather than always running the most expensive pass.
- **Temperature** is not an LLM sampling parameter — it is a measure of retrieval uncertainty that governs the explore/exploit tradeoff.
- **Concept nodes** and **coactivation** tracking echo Hofstadter's interest in how repeated exposure causes abstract concepts to crystallize from concrete examples.

LCMA adapts these ideas to a knowledge management context rather than a perceptual analogy context. The scouts probe a knowledge base instead of a letter-string workspace, and the “codelets” are retrieval strategies rather than micro-transformations. But the core insight — that intelligent retrieval requires parallel, temperature-governed search with emergent structure — carries over directly.

### Overview

LCMA turns Lithos from “notes + embeddings” into a **cognitive substrate**:

- **Parallel Terraced Scan (PTS)** retrieval: many cheap probes → selective deepening
- **Typed, weighted graph edges** that strengthen _and weaken_ over time
- **Working Memory (WM)** and **Long-Term Memory (LTM)** split
- **Concept nodes** that emerge from stable clusters (with damping to avoid domination)
- **Multi-agent coordination** via namespaces, scopes, and task-shared WM
- **Auditable retrieval** (“why was this retrieved?” receipts)
- **Two-component architecture**: `lithos-enrich` (in-process background enrichment worker) + `lithos_retrieve` (lightweight query-time orchestration)

The architecture is split into two runtime components: **`lithos-enrich`** runs as an in-process background worker inside the Lithos server, performing expensive query-independent work (concept node creation, typed edge building, salience score updates, coactivation tracking, background LLM synthesis). Its outputs are persistent artifacts discoverable via `lithos_search`. **`lithos_retrieve`** is a lightweight, synchronous MCP tool that assembles pre-computed enrichment into a ranked result for a specific query — fast because the heavy lifting already happened in the background.

### Non-goals

- Training model weights
- Fully automatic truth resolution
- Heavy central DB dependence (local-first remains the default)

---


## 1.1 Core Objects

### Memory Node

A `KnowledgeDocument` — an Obsidian-compatible Markdown note with YAML frontmatter. The existing Lithos schema defines these fields; LCMA extends them.

**Existing fields (preserved):**

- `id` (UUID), `path` (relative to `knowledge/` dir)
- `title`, `author` (immutable creator)
- `created_at`, `updated_at`
- `content` (markdown body, without frontmatter)
- `tags`, `aliases` (for wiki-link resolution)
- `confidence` (float 0–1, default 1.0 — author's belief, not retrieval utility)
- `contributors` (append-only list of editing agents)
- `source` (task ID or provenance reference)
- `supersedes` (UUID of document this replaces)
- `links` (parsed `[[wiki-links]]` from content)

**LCMA adds (optional, with defaults so existing notes remain valid):**

- `schema_version` (int, default 1)
- `namespace` (str, derived from path if absent, e.g. `"project/lithos"`)
- `access_scope` (enum: `shared|task|agent_private`, default `shared`) — **advisory visibility**, not a security control (see trust model note below). Project-level scoping is handled by `namespace`, not `access_scope` — this avoids overlap between the two fields.
- `note_type` (enum: `observation|agent_finding|summary|concept|task_record|hypothesis`, default `observation`)
- `entities` (list of extracted entity names) — **deferred to MVP 2**: auto-extracted by `lithos-enrich`; not populated in MVP 1
- `status` (enum: `active|archived|quarantined`, default `active`)
- `summaries` (optional nested object with `short` and `long` fields)

Implementation notes:

- `summaries` is stored in YAML as a nested mapping, not dotted keys.
- LCMA frontmatter fields are additive and backward-compatible.
- `namespace` defaults from the document path when absent and may be synthesized at read time for legacy notes rather than eagerly written.

**Namespace derivation rule** (when `namespace` is absent from frontmatter):

The namespace is derived from the note's path relative to `knowledge/` by taking the directory portion. Recognized conventions get short names; everything else uses the full directory path:

| Path | Derived namespace |
| --- | --- |
| `knowledge/shared/foo.md` | `shared` |
| `knowledge/project/lithos/bar.md` | `project/lithos` |
| `knowledge/agent/cartographer/scratch.md` | `agent/cartographer` |
| `knowledge/research/foo.md` | `research` |
| `knowledge/research/deep/nested/bar.md` | `research/deep/nested` |
| `knowledge/foo.md` (root-level) | `default` |

Rules: (1) strip `knowledge/` prefix, (2) take the directory path (everything before the filename), (3) if empty (root-level note), use `"default"`. The namespace is always a forward-slash-separated string, never includes the filename, and is case-preserved. Explicitly set `namespace` in frontmatter overrides path derivation.

**Trust model for `access_scope`:** `access_scope` is an advisory visibility mechanism to reduce noise, not a security control. Agents self-identify via `agent_id` parameter on `lithos_retrieve`. `agent_private` notes are filtered from other agents' results by matching `author == agent_id`; `task`-scoped notes are filtered to agents working on the same `task_id`. Neither is enforced cryptographically — agents could misidentify themselves. The purpose is to keep agent-specific scratch notes from bloating search results for all agents, not to enforce hard isolation. Project-level scoping is handled by `namespace` filtering, not by `access_scope`.

**Dynamic signals (stored in `stats.db`, NOT in frontmatter):**

- `salience` (float, default 0.5 — retrieval utility, complementary to `confidence`)
- `usage_stats` (retrieval_count, last_used, coactivation counts)
- embeddings managed by ChromaDB (see §4.5)

### Edge

LCMA introduces typed, weighted edges stored in `edges.db`. These are **separate from** the existing wiki-link graph (NetworkX DiGraph), which continues to handle `[[wiki-link]]` relationships, link resolution, and the `links` section of `lithos_related`.

LCMA edge fields:

- `from_id`, `to_id`
- `type`: `related_to`, `supports`, `contradicts`, `is_example_of`, `depends_on`, `analogy_to`, etc.
- `weight` (float)
- `provenance` (who created it: user/agent/rule — mirrors the `author`/`contributors` pattern on notes)
- `evidence` (anchors/snippets)

Provenance authority rule (cross-plan consistency):

- Canonical declared lineage is `derived_from_ids` in note frontmatter (Digest Auto-Link v2).
- `edges.db` may mirror lineage as `type="derived_from"` for retrieval/ranking performance, but this is a projection.
- If frontmatter and `edges.db` diverge, frontmatter is authoritative and projection must be repaired/rebuilt.

### Concept Node (emergent)

Concepts emerge in two phases. First, `lithos-enrich` detects stable coactivation clusters and tracks them as **derived clusters** in `stats.db` — these are queryable but not yet materialized as notes. Second, when a cluster is stable enough (configurable threshold), an agent or `lithos-enrich` can **promote** it to a regular `KnowledgeDocument` with `note_type: "concept"`, created via `lithos_write`. This avoids auto-generating markdown notes that blur the authoritative/derived boundary and create noisy git churn. Once materialized, a concept note summarizes its cluster and links canonical examples via `is_example_of` edges in `edges.db`.

## 1.2 Retrieval: PTS-lite

PTS is exposed as a **new** `lithos_retrieve` tool. The existing `lithos_search` tool remains available for direct use by clients.

**Scouts** (parallel candidate generators, mapped to existing infrastructure):

MVP 1 scouts:

- vector similarity top-k → `ChromaIndex.search()` (existing)
- lexical match → `TantivyIndex.search()` (existing)
- tags/metadata → `KnowledgeManager.list_all()` with filters (existing)
- recency → sort by `updated_at` from knowledge manager (existing)
- provenance → walk `derived_from_ids` forward and reverse links (existing `KnowledgeMetadata.derived_from_ids` + provenance index)
- exact/alias/path → match by title, alias, slug, filename, or path via existing `KnowledgeGraph` link resolution (`_alias_to_node`, slugified title, UUID, path matching)
- task-context → notes linked by the same `task_id` via findings and claims in `coordination.db` (existing indexes); activated when `task_id` is provided
- freshness → retrieve stale-but-relevant notes using existing `expires_at` / `is_stale`; activates more strongly when query text contains update/refresh/recheck/verify signals

MVP 2 scouts:

- graph neighbors → `KnowledgeGraph.get_links()` (NetworkX, existing) + `edges.db` queries (new) — deferred to MVP 2 because edges.db needs population first
- coactivation/bridge → find nodes that frequently co-occur or connect otherwise separate clusters via `coactivation` table in `stats.db` — needs MVP 1 coactivation data to be meaningful
- source-url/domain → notes from the same normalized URL family or host via existing `_source_url_to_id` map and `normalize_url()`; activated when query or seed nodes have `source_url` set

MVP 3 scouts:

- analogy → new (frame extraction, no existing equivalent)
- exploration → new (novelty/random sampling, temperature-guided)

> **Architecture note**: `lithos_retrieve` is fast enough for synchronous client use because `lithos-enrich` has already performed the expensive enrichment work in the background. **Background LLM synthesis is NOT part of `lithos_retrieve`** — it belongs to `lithos-enrich`, which runs asynchronously. `lithos_retrieve` only executes Terrace 0 and Terrace 1.

**Terraces** (query-time, inside `lithos_retrieve`):

- Terrace 0: union candidates (cheap)
- Terrace 1: fast re-rank (diversity + priors)

**Background synthesis** (query-independent, inside `lithos-enrich`):

- `llm_interpretive_synthesize()` — proactive knowledge synthesis via LLM, run asynchronously in the background by `lithos-enrich`. This is **not a retrieval terrace** — it produces persistent artifacts (summaries, concept notes, edge annotations) that improve future retrieval, rather than reranking a specific query's results.

## 1.3 Learning (v1)

- reinforce edges between nodes that co-occur in successful contexts
- periodic summarization/distillation
- concept node creation from repeated co-activation clusters
- aging/decay of unused nodes/edges

## 1.4 Agents (v1 roles)

Lithos already has a full agent coordination system: agent registry, task creation, aspect-based claims with TTL, and findings. LCMA agent roles build on top of this — they are registered via the existing `lithos_agent_register` tool using the `type` field.

- Retriever, Interpreter, Librarian, Cartographer, Auditor

## 2.1 What v2 Adds

### A) Negative feedback and failure signals

LCMA must be able to **lose confidence**, not only gain it.

**Add bidirectional learning:**

- **Positive reinforcement**: strengthen edges / salience when helpful
- **Negative reinforcement**: weaken edges / priors when repeatedly irrelevant or misleading

Key rule: apply penalties **contextually by namespace** before global decay.

### B) Contradiction workflow is first-class

Contradictions create structured “conflict objects” with states:

- `unreviewed | accepted_dual | superseded | refuted | merged`

This extends the existing `supersedes` frontmatter field. When a conflict is resolved as `superseded`, the winning note can set `supersedes` on the losing one.

Policy:

- retrieval surfaces conflicts when relevant
- resolution is explicit (agent/user), not silent

### C) Note types are first-class

Add `note_type` and type-specific behavior:

- `observation`
- `agent_finding`
- `summary`
- `concept`
- `task_record`
- `hypothesis`

Each type defines:

- retrieval prior
- decay curve
- whether it can be auto-edited
- contradiction precedence rules (limited)

### D) Add Analogical Reasoning Scout

Add a scout aimed at **structural similarity**, not semantic closeness:

- pattern templates + “frame extraction” from notes
- match frames: `{problem, constraints, actions, outcome, lessons}`
- graph motif similarity and tag-pattern overlap

### E) Multi-agent coordination is architectural

Lithos already provides: agent registry (`lithos_agent_register`), task lifecycle (`lithos_task_create/complete`), aspect-based claims with TTL (`lithos_task_claim/renew/release`), and findings (`lithos_finding_post/list`). LCMA extends this with:

- namespaces and memory visibility (new filtering layer on existing tools)
- task-shared WM (linked to existing `task_id` from coordination) — per-agent WM is deferred; task-shared WM maps directly onto existing coordination and is simpler to reason about
- write policies for reinforcement into shared memory (can gate via existing claim mechanics)

Implementation note:

- In MVP 1, namespace/access-scope enforcement is guaranteed on `lithos_retrieve`. Existing tools such as `lithos_read`, `lithos_search`, and `lithos_list` remain backward-compatible and do not gain caller-context-aware access control in the first slice.

### F) Cold-start / bootstrap behavior

When no weights/stats exist:

- rely on lexical + tags + explicit links + recency
- keep embeddings helpful but not dominant
- bootstrap concept nodes via curated summaries or agent-assisted clustering

### G) Embedding model versioning

Support multiple embedding spaces:

- store `embedding_space_id` per embedding
- migrate via lazy re-embed or batch job
- retrieval can query multiple spaces during transition

### H) Access control / privacy scoping

At retrieval time:

- namespace filter is applied at scout generation
- scope rules prevent cross-project leakage
- “shared patterns” can be opt-in allowlisted

MVP 1 boundary:

- Scope filtering is implemented in `lithos_retrieve` and its scouts first.
- Existing direct-access tools remain unchanged until a separate caller-context contract is introduced.

### I) Temperature is operationalized

Per query:

- compute coherence score among top candidates
- define temperature = 1 - coherence
- temperature controls exploration weight and terrace depth

### J) Concept node damping

Prevent rich-get-richer domination:

- salience ceiling per concept node
- diversity penalties for repeated concept retrieval
- concept nodes act as **gateways** to specifics (retrieve both)

### K) Robust success metrics

Don’t rely on LLM self-report alone. Use multi-signal proxy:

- citations/links to retrieved notes
- similarity between output and note content
- user acceptance/edit signals (where available)
- follow-up retrieval locality

### L) WM→LTM consolidation

Batch update during “rest”:

- reinforce edges among WM items that recur
- promote hypotheses that were validated
- update summaries and concept nodes

### M) Auditor is specified

Every retrieval produces a structured “receipt” log entry, queryable later.

### N) Schema versioning

Add `schema_version` per note and a migration registry.

---

# 3) System Architecture

## 3.1 Layers

1. **Storage layer (existing Lithos vault + LCMA stores)**
	- Markdown notes + frontmatter in `data/knowledge/` (existing `KnowledgeManager`)
	- All SQLite databases and LCMA stores in `data/.lithos/`: coordination, edges, stats, receipts, migrations

2. **Index layer (existing engines + new stores)**
	- Tantivy full-text index in `data/.tantivy/` (existing, English stemmer)
	- ChromaDB embeddings in `data/.chroma/` (existing, `all-MiniLM-L6-v2`)
	- NetworkX wiki-link graph in `data/.graph/` (existing, JSON-cached as `graph.json`)
	- LCMA typed edges in `data/.lithos/edges.db` (new)

3. **Retrieval layer (PTS — new, alongside existing tools)**
	- New `lithos_retrieve` tool orchestrates scouts
	- Existing `lithos_search` remains as a direct-access tool
	- Terraces re-rank and optionally interpret

4. **Learning layer (new)**
	- reinforcement/penalties (via `stats.db`)
	- consolidation and decay
	- concept formation

5. **Governance layer (extends existing coordination)**
	- namespaces and access control (new filtering on existing tools)
	- contradiction workflow (new, extends existing `supersedes` field)
	- provenance and audit receipts (`receipts` table in `stats.db`)


#### Two-Component Runtime Architecture

LCMA has two runtime components:

- **`lithos_retrieve` (MCP tool, synchronous)**: Terrace 0 + Terrace 1 only. Uses pre-computed enrichment from `stats.db` and `edges.db`. Fast enough for the client hot path.
- **`lithos-enrich` (in-process worker)**: Runs consolidation, concept formation, background LLM synthesis, decay, and edge reinforcement. Triggered via two modes:
  - **Event-driven incremental**: Runs inside the Lithos server process and subscribes to the existing in-memory event bus (`events.py`). All state-mutating actions emit events via the bus: `lithos_write` (`note.created`/`note.updated`), `lithos_delete` (`note.deleted`), `lithos_task_complete` (`task.completed`), `lithos_finding_post` (`finding.posted`), and `lithos_edge_upsert` (`edge.upserted` — **new event type**, added to `events.py` in MVP 1). The enrich worker consumes these events and writes to `enrich_queue`. A periodic drain (e.g., every 5 minutes) processes pending queue entries, targeting only affected nodes. Multiple events for the same node are deduplicated within a drain cycle.
  - **Scheduled full sweep** (default: daily, configurable): Runs a full enrichment pass across all nodes regardless of queue state — recomputes decay, runs full concept cluster analysis, and catches anything missed by incremental runs.
  - Writes persistent artifacts (concept nodes, edges, salience scores) discoverable via `lithos_search`.
  - The incremental path is intentionally best-effort; the scheduled full sweep is authoritative and repairs anything missed due to lossy event delivery.

---

# 4) Minimal On-Disk Representation

This is intentionally “minimum viable” while supporting v1 + v2.

## 4.1 Directory Layout

The existing Lithos directory structure is preserved. LCMA consolidates all SQLite databases under `data/.lithos/`. `coordination.db` is already migrated by current code and should not be re-migrated in LCMA work.

```
data/                              # configurable via StorageConfig.data_dir
  knowledge/                       # existing: Markdown notes (subdirs allowed via `path` param)
    shared/                        #   optional namespace convention
    project/<project_name>/        #   optional namespace convention
    agent/<agent_id>/              #   optional namespace convention
  .tantivy/                        # existing: Tantivy full-text index
  .chroma/                         # existing: ChromaDB embeddings (all-MiniLM-L6-v2)
  .graph/                          # existing: NetworkX wiki-link graph (JSON-cached as graph.json)
  .lithos/                         # SQLite stores + LCMA data
    coordination.db                #   agents, tasks, claims, findings
    edges.db                       #   typed weighted edges (separate from NetworkX)
    stats.db                       #   usage stats, salience, decay, receipts, WM
    migrations/
      registry.json                #   schema migrations
```

Note: namespace-based subdirectories under `knowledge/` are a **convention**, not enforced. The existing `lithos_write` `path` parameter already supports arbitrary subdirectories.

### Why sqlite for LCMA stores?

- still local-first
- fast updates for weights/stats
- avoids rewriting large markdown files just to adjust edge weights
- consistent with the existing `coordination.db` pattern (all SQLite databases live together in `data/.lithos/`)

## 4.2 Note Format (Markdown + frontmatter)

Notes use the existing `KnowledgeDocument` format with LCMA fields added as **optional extensions**. Existing notes without LCMA fields remain valid — defaults are applied at read time.

Example: `knowledge/project/lithos/memory/LCMA.md`

```yaml
---
# --- Existing Lithos fields (preserved) ---
id: "7f3a9c12-4d5e-6f7a-8b9c-0d1e2f3a4b5c"   # UUID (existing format)
title: "Lithos Cognitive Memory Architecture"
author: "agent-cartographer"                     # immutable original creator
created_at: "2026-02-26T18:00:00Z"
updated_at: "2026-02-26T18:10:00Z"
tags: ["lithos", "memory", "pts", "agents"]
aliases: ["LCMA", "cognitive memory"]            # for wiki-link resolution
confidence: 0.7                                  # author's belief (0-1)
contributors: ["agent-librarian"]                # append-only edit trail
source: "task_abc123"                            # originating task ID
supersedes: null                                 # UUID of replaced document

# --- LCMA extensions (optional, with defaults) ---
schema_version: 2                                # default: 1
namespace: "project/lithos"                      # default: derived from path
access_scope: "shared"                            # default: "shared" (enum: shared|task|agent_private)
note_type: "concept"                             # default: "observation"
entities: ["Lithos", "Parallel Terraced Scan", "Hofstadter"]
status: "active"                                 # default: "active"
summaries:
  short: "PTS-style retrieval and learning for Lithos."
  long: "..."
---
# Lithos Cognitive Memory Architecture
...
```

Note: `salience`, `usage_stats`, and `embedding_spaces` are stored in `stats.db` and ChromaDB respectively, not in frontmatter. This avoids constant frontmatter rewrites from learning updates.

## 4.3 Edges Store Schema (sqlite)

Stored in `data/.lithos/edges.db`. This is **separate from** the existing NetworkX wiki-link graph (`data/.graph/graph.json`), which continues to power the `links` section of `lithos_related` and wiki-link resolution.

The two systems complement each other:

- **NetworkX**: structural `[[wiki-link]]` navigation, link resolution by path/filename/UUID/alias, broken link detection
- **edges.db**: semantic/learned relationships with weights, types, and provenance

Table: `edges`

- `edge_id` TEXT PK
- `from_id` TEXT
- `to_id` TEXT
- `type` TEXT
- `weight` REAL
- `namespace` TEXT
- `created_at` TEXT
- `updated_at` TEXT
- `provenance_actor` TEXT (agent/user/rule id)
- `provenance_type` TEXT (human|agent|rule)
- `evidence` TEXT (JSON: anchors/snippets)
- `conflict_state` TEXT NULL (for `contradicts` edges)

Indexes:

- `(from_id)`, `(to_id)`, `(type)`, `(namespace)`
- optionally `(from_id, type)` for speed

New MCP tools for LCMA edges (do not modify the `links` section of `lithos_related`):

- `lithos_edge_upsert` — create or update a typed edge (combines former `lithos_edge_create` + `lithos_edge_update`)
- `lithos_edge_list` — query edges by node, type, or namespace
- `lithos_conflict_resolve` — resolve a contradiction edge (**deferred to MVP 2**, when contradiction workflow is live)

## 4.4 Stats Store Schema (sqlite)

Table: `node_stats`

- `node_id` TEXT PK
- `salience` REAL
- `retrieval_count` INTEGER
- `last_retrieved_at` TEXT
- `last_used_at` TEXT
- `ignored_count` INTEGER
- `misleading_count` INTEGER
- `decay_rate` REAL
- `spaced_rep_strength` REAL


Table: `coactivation`

- `node_id_a` TEXT
- `node_id_b` TEXT
- `namespace` TEXT
- `count` INTEGER
- `last_at` TEXT  
    PK `(node_id_a, node_id_b, namespace)`

Table: `enrich_queue`

- `id` INTEGER PK (autoincrement)
- `trigger_type` TEXT — `'note.created'`, `'note.updated'`, `'note.deleted'`, `'task.completed'`, `'finding.posted'`, `'edge.upserted'`, `'full_sweep'`
- `node_id` TEXT NULL — affected node UUID (if applicable)
- `task_id` TEXT NULL — affected task ID (if applicable)
- `triggered_at` TEXT — ISO datetime
- `processed_at` TEXT NULL — NULL = pending; set when processed

Index: `(processed_at)` for fast pending-item queries.

Deduplication: the drain loop handles two work types separately:

1. **Node-level work**: `SELECT DISTINCT node_id FROM enrich_queue WHERE node_id IS NOT NULL AND processed_at IS NULL` — multiple events for the same node are processed once per drain cycle.
2. **Task-level work**: `SELECT DISTINCT task_id FROM enrich_queue WHERE node_id IS NULL AND processed_at IS NULL` — each completed task triggers its own consolidation pass, avoiding NULL-collapse from `DISTINCT node_id`.

Table: `working_memory`

- `task_id` TEXT
- `node_id` TEXT
- `activation_count` INTEGER — how many times this node was retrieved/used in this task
- `first_seen_at` TEXT — ISO datetime of first retrieval in this task
- `last_seen_at` TEXT — ISO datetime of most recent retrieval in this task
- `last_receipt_id` TEXT NULL — backreference to `receipts` table for traceability
    PK `(task_id, node_id)`

Index: `(task_id)` for fast WM lookup per task.

Working memory is the operational data structure for task-scoped retrieval tracking. The `receipts` table in `stats.db` remains the audit/explainability log but is not used for keyed lookups. Consolidation (§5.7) reads from `working_memory` instead of scanning `receipts`.

**WM eviction policy**: The daily full sweep in `lithos-enrich` evicts `working_memory` entries where: (a) the associated `task_id` maps to a completed or cancelled task in `coordination.db`, OR (b) `last_seen_at` exceeds a configurable TTL (default: 7 days, via `LithosConfig.lcma.wm_eviction_days`). This prevents unbounded WM growth from crashed or abandoned agent tasks.

Table: `receipts`

- `id` TEXT PK — receipt ID (e.g., `rcpt_7f3a9c12`)
- `ts` TEXT — ISO datetime
- `query` TEXT — the query text
- `namespace_filter` TEXT — JSON array of namespace strings
- `agent_id` TEXT NULL
- `task_id` TEXT NULL
- `surface_conflicts` INTEGER — boolean (0/1)
- `temperature` REAL
- `scouts_fired` TEXT — JSON array of scout names
- `candidates_considered` INTEGER
- `terrace_reached` INTEGER
- `final_nodes` TEXT — JSON array of `{id, reason}` objects
- `conflicts_surfaced` TEXT — JSON array of `{edge_id, state}` objects

Indexes: `(ts)`, `(task_id)`, `(agent_id)` for filtered queries via `lithos_receipts`.

## 4.5 Embeddings Store

Lithos already uses **ChromaDB** (`data/.chroma/`) with `all-MiniLM-L6-v2` (sentence-transformers) and cosine similarity. Documents are chunked (~500 char target, ~1000 max) and stored with metadata (`doc_id`, `chunk_index`, `title`, `path`, `author`, `tags`).

LCMA extends this with embedding space versioning via **separate ChromaDB collections** per space:

- Current collection: `knowledge` (existing, preserved)
- Versioned collections: `knowledge_<embedding_space_id>` (e.g., `knowledge_emb_v2_2026`)
- Active spaces tracked in config (not per-note frontmatter — avoids churn on re-embedding)
- During migration, the vector scout queries multiple collections and merges results

This avoids introducing a separate SQLite or FAISS store — ChromaDB already handles ANN search, persistence, and cosine similarity.

## 4.6 Retrieval Receipts (Auditor)

Receipts are stored in the `receipts` table in `stats.db` (schema in §4.4). This provides indexed queries for `lithos_receipts` without full-file scans, and is consistent with all other LCMA stores using SQLite.

Example receipt row:

```json
{
  "id": "rcpt_7f3a9c12",
  "ts": "2026-02-26T18:12:01Z",
  "query": "add dynamic memory management to lithos",
  "namespace_filter": ["project/lithos", "shared"],
  "surface_conflicts": true,
  "temperature": 0.42,
  "scouts_fired": ["vector", "lexical", "graph"],
  "candidates_considered": 97,
  "terrace_reached": 1,
  "final_nodes": [
    {"id": "node_7f3a9c", "reason": "concept gateway + lexical match + high salience (damped)"}
  ],
  "conflicts_surfaced": [{"edge_id": "edge_19cc", "state": "unreviewed"}]
}
```

JSON array fields (`namespace_filter`, `scouts_fired`, `final_nodes`, `conflicts_surfaced`) are stored as JSON text columns in SQLite and parsed on read.

---

# 5) Pseudocode

The pseudocode is designed so you can implement MVP first, then add sophistication.

## 5.1 Types

```python
class QueryContext:
    query_text: str
    namespace_filter: list[str]
    agent_id: str
    task_id: str | None
    surface_conflicts: bool = False  # when True, surface contradiction edges in results
    max_context_nodes: int

class Candidate:
    """Internal working type during PTS pipeline. Projected to ResultItem on output."""
    node_id: str
    score: float                # normalized 0-1 (all scout scores normalized during candidate creation)
    reasons: list[str]
    scouts: list[str]

class ResultItem:
    """A single result — superset of lithos_search result fields.

    `score` is always a normalized float matching the hybrid-mode SearchResult shape.
    Semantic similarity scores are mapped to `score` during candidate creation.
    This ensures clients can use lithos_retrieve and lithos_search interchangeably
    without handling different score field names.
    """
    # lithos_search fields (all preserved):
    id: str
    title: str
    snippet: str
    score: float                # always normalized (hybrid-mode SearchResult compatible)
    path: str
    source_url: str
    updated_at: str
    is_stale: bool
    derived_from_ids: list[str]
    # LCMA-only extras (additive):
    reasons: list[str]   # why this node was retrieved
    scouts: list[str]    # which scouts surfaced it
    salience: float      # from stats.db

class RetrievalResult:
    """Response shape is structurally compatible with lithos_search.

    The top-level `results` key mirrors lithos_search so clients can
    switch between tools without rewriting result-handling code.
    LCMA-specific fields (reasons, scouts, salience, temperature,
    terrace_reached, receipt_id) are additive — clients that only
    read id/title/score/snippet will work unchanged.
    """
    results: list[ResultItem]   # compatible with lithos_search results
    temperature: float
    terrace_reached: int
    receipt_id: str             # reference to receipts table entry in stats.db
```

---

## 5.2 Scout Interface

Each scout wraps existing Lithos infrastructure where possible. Implementation notes in comments.

```python
# --- MVP 1 scouts ---

def scout_vector(q: QueryContext, k: int) -> list[Candidate]:
    # Wraps existing ChromaIndex.search(query, limit=k, threshold, tags)
    # Queries active embedding spaces (multiple ChromaDB collections during migration)
    ...

def scout_lexical(q: QueryContext, k: int) -> list[Candidate]:
    # Wraps existing TantivyIndex.search(query, limit=k, tags, author, path_prefix)
    ...

def scout_tags_meta(q: QueryContext, k: int) -> list[Candidate]:
    # Wraps existing KnowledgeManager.list_all(tags=..., author=..., path_prefix=...)
    ...

def scout_recency(q: QueryContext, k: int) -> list[Candidate]:
    # Wraps existing KnowledgeManager.list_all(since=...) sorted by updated_at
    ...

def scout_provenance(q: QueryContext, seed_nodes: list[str], k: int) -> list[Candidate]:
    # Walk derived_from_ids forward (what was derived from these seeds?)
    # and reverse (what did these seeds derive from?).
    # Uses existing KnowledgeMetadata.derived_from_ids and provenance index
    # (KnowledgeManager._build_provenance_index; same traversal exposed via
    # the provenance section of lithos_related).
    # Cheap and high-signal: declared lineage is the strongest relationship signal
    # in the knowledge base.
    ...

def scout_exact_alias(q: QueryContext, k: int) -> list[Candidate]:
    # Exact match by title, alias, slug, filename, or path.
    # Wraps existing KnowledgeGraph link resolution:
    #   - _alias_to_node dict (alias -> node_id)
    #   - slugified title matching (title.lower().replace(" ", "-"))
    #   - UUID prefix matching
    #   - path/filename matching
    # Handles the common case where an agent knows roughly what it's looking
    # for by name — other scouts assume fuzzy/semantic intent.
    ...

def scout_task_context(q: QueryContext, k: int) -> list[Candidate]:
    # Notes linked to this task. Only activated when q.task_id is provided.
    #
    # MVP 1 sources (implemented):
    #   1. Notes referenced in findings for this task — pulled from
    #      CoordinationService.list_findings(task_id) where finding.knowledge_id
    #      is set. Uses idx_findings_task_id.
    #   2. Notes whose frontmatter `source` field equals task_id — i.e. notes
    #      that were authored *for* this task and recorded the task as their
    #      provenance source.
    #
    # MVP 2+ sources (deferred): notes "linked by claimed aspects" requires
    # an aspect→note linkage that does not yet exist in coordination.db. A
    # prior implementation that pulled in *every* note authored by *any*
    # agent with a non-expired claim on the task was removed because it
    # flooded results with unrelated notes from long-lived agents — wait
    # for a real aspect→note index before re-introducing claim-based context.
    ...

def scout_freshness(q: QueryContext, k: int) -> list[Candidate]:
    # Retrieve stale-but-relevant notes using existing expires_at / is_stale.
    # Activates more strongly when query text contains update/refresh/recheck/
    # verify/latest signals (simple keyword check).
    # Uses existing KnowledgeMetadata.is_stale property and expires_at field.
    # Purpose: surface notes that need attention, not just notes that match.
    ...

# --- MVP 2 scouts ---

def scout_graph(q: QueryContext, seed_nodes: list[str], k: int) -> list[Candidate]:
    # Queries BOTH:
    #   - existing KnowledgeGraph.get_links(id, direction, depth) for wiki-link neighbors
    #   - new edges.db for typed/weighted edge neighbors
    # Deferred to MVP 2: edges.db needs population from MVP 1 usage first.
    ...

def scout_coactivation(q: QueryContext, seed_nodes: list[str], k: int) -> list[Candidate]:
    # Find nodes that frequently co-occur with seed nodes or connect otherwise
    # separate clusters, via coactivation table in stats.db.
    # Needs MVP 1 coactivation data to be meaningful.
    # More principled than random exploration: surfaces empirically useful
    # associations rather than hoping for serendipity.
    ...

def scout_source_url(q: QueryContext, seed_nodes: list[str], k: int) -> list[Candidate]:
    # Notes from the same normalized URL family or host.
    # Wraps existing _source_url_to_id map and normalize_url().
    # Only activated when query or seed nodes have source_url set.
    # Most useful for research/digest-heavy knowledge bases.
    ...

# --- MVP 3 scouts ---

def scout_analogy(q: QueryContext, k: int) -> list[Candidate]:
    # NEW: no existing equivalent — frame extraction + structural matching
    ...

def scout_exploration(q: QueryContext, k: int, mode: str) -> list[Candidate]:
    # NEW: mode in {"novelty","random","mixed"}, temperature-guided
    ...

def scout_contradictions(q: QueryContext, seed_nodes: list[str]) -> list[str]:
    # Queries edges.db for type="contradicts" edges
    ...
```

Notes:

- All scouts must apply `namespace_filter` and `access_scope` gating **before returning** candidates.
- In MVP 1, this gating is local to `lithos_retrieve`; legacy tools keep their current behavior until explicit caller-context support is added.
- Existing search tools (`lithos_search`) remain available for direct client use.
    

---

## 5.3 Temperature (Coherence)

Reviewer suggestion, implemented as coherence among top candidates (by edges).

**MVP 1 implementation note.** `compute_temperature` in `src/lithos/lcma/retrieve.py` unconditionally returns `LcmaConfig.temperature_default` (0.5). The `edge_store` argument is preserved in the signature for forward compatibility but never read. Coherence-based computation activates in MVP 3 once `edges.db` has enough typed edges for `compute_coherence` to be meaningful.

```python
def compute_coherence(top_node_ids: list[str], namespace: str) -> float:
    # coherence in [0,1]
    # mean normalized edge strength among pairs (including inferred via shared concept links if desired)
    pairs = all_pairs(top_node_ids)
    strengths = []
    for a,b in pairs:
        w = graph_edge_strength(a, b, namespace)   # 0..1
        strengths.append(w)
    if not strengths:
        return 0.0
    return mean(strengths)

def compute_temperature(coherence: float) -> float:
    return 1.0 - coherence
```

---

## 5.3.1 Score Contract

**Problem**: Raw scores from different backends have incompatible semantics — BM25 scores are unbounded positive floats, cosine similarity is 0–1, and RRF scores are small positive fractions (e.g., max ~0.033 with k=60 and 2 lists). The existing `lithos_search` hybrid mode returns raw RRF scores, and tests only assert `score > 0`.

**`lithos_retrieve` score contract**: All `ResultItem.score` values are normalized to `[0, 1]` via per-scout min-max normalization within each `merge_and_normalize` call. This makes scores comparable across scouts and meaningful to clients.

**`lithos_search` is unchanged**: Its scores retain their current semantics (raw BM25, cosine, or RRF depending on mode). `lithos_retrieve` normalizes internally — it does not change `lithos_search` behavior.

```python
def merge_and_normalize(candidates: list[Candidate]) -> list[Candidate]:
    """Merge candidates by node_id (max score wins) and normalize scores to [0, 1].

    Normalization is per-scout min-max: within each scout's candidates,
    the highest score maps to 1.0 and the lowest to 0.0 (or 1.0 if all
    scores are equal). This makes scores comparable across scouts with
    different raw score ranges (BM25, cosine, RRF, binary match, etc.).
    """
    # Group by scout, normalize each group
    by_scout: dict[str, list[Candidate]] = group_by_scout(candidates)
    normalized = []
    for scout_name, group in by_scout.items():
        scores = [c.score for c in group]
        lo, hi = min(scores), max(scores)
        span = hi - lo if hi > lo else 1.0
        for c in group:
            c.score = (c.score - lo) / span
            normalized.append(c)

    # Merge by node_id: keep max normalized score, union reasons and scouts
    merged: dict[str, Candidate] = {}
    for c in normalized:
        if c.node_id in merged:
            existing = merged[c.node_id]
            existing.score = max(existing.score, c.score)
            existing.reasons += c.reasons
            existing.scouts = list(set(existing.scouts + c.scouts))
        else:
            merged[c.node_id] = c
    return list(merged.values())
```

**Prerequisite**: This normalization must be implemented before `lithos_retrieve` ships in MVP 1. It is not a change to `lithos_search`.

---

## 5.4 Retrieval: PTS Terraces

```python
def retrieve_pts(q: QueryContext) -> RetrievalResult:
    receipt = init_receipt(q)

    # -------- Terrace 0: candidate generation --------
    # Phase A — truly parallel scouts (no dependencies):
    cands = []
    cands += scout_vector(q, k=12)
    cands += scout_lexical(q, k=12)
    cands += scout_exact_alias(q, k=6)            # MVP 1: title/alias/path exact match
    cands += scout_tags_meta(q, k=8)
    cands += scout_recency(q, k=6)
    cands += scout_freshness(q, k=6)              # MVP 1: stale-but-relevant notes
    if q.task_id:
        cands += scout_task_context(q, k=8)       # MVP 1: same-task findings/claims

    # MVP 3: analogy + exploration scouts added here

    pool = merge_and_normalize(cands)             # merges by node_id, normalizes per scout
    receipt["candidates_considered"] = len(pool)
    receipt["scouts_fired"] = scouts_used(pool)

    # Phase B — sequential (needs Phase A results as seeds):
    seed = top_ids(pool, n=10)
    pool += merge_and_normalize(scout_provenance(q, seed, k=12))  # MVP 1: derived_from lineage
    pool += merge_and_normalize(scout_graph(q, seed, k=18))       # MVP 2: wiki-link + edges.db
    pool += merge_and_normalize(scout_coactivation(q, seed, k=8)) # MVP 2: co-occurrence bridges
    pool += merge_and_normalize(scout_source_url(q, seed, k=6))   # MVP 2: same-source notes

    # -------- Terrace 1: fast re-rank (no LLM) --------
    ranked = rerank_fast(q, pool)                 # diversity, priors, concept damping, type priors
    top = ranked[:30]

    # Temperature: use fixed default during cold start (few edges → unreliable coherence)
    total_edges = count_edges_in_namespace(dominant_namespace(q))
    if total_edges < TEMPERATURE_EDGE_THRESHOLD:  # default: 50, configurable via LcmaConfig
        temp = TEMPERATURE_DEFAULT  # default: 0.5
    else:
        coherence = compute_coherence([c.node_id for c in top[:12]], namespace=dominant_namespace(q))
        temp = compute_temperature(coherence)
    receipt["temperature"] = temp

    # Adjust exploration based on temperature (MVP 3 — requires scout_exploration)
    if temp > 0.6 and total_edges >= TEMPERATURE_EDGE_THRESHOLD:
        pool += merge_and_normalize(scout_exploration(q, k=8, mode="mixed"))
        ranked = rerank_fast(q, pool)
        top = ranked[:30]

    # Contradictions check (MVP 1: no-op stub returning []; activated in MVP 2)
    conflict_edges = scout_contradictions(q, seed_nodes=[c.node_id for c in top[:10]])
    receipt["conflicts_surfaced"] = conflict_edges

    # Background LLM synthesis is NOT part of lithos_retrieve.
    # It belongs to lithos-enrich, which runs asynchronously in the background.
    # terrace_reached will always be 0 or 1 for lithos_retrieve.
    terrace = 1
    final_nodes = top[:q.max_context_nodes]

    # -------- Working Memory upsert (if task-scoped) --------
    if q.task_id is not None:
        for c in final_nodes:
            upsert_working_memory(
                task_id=q.task_id,
                node_id=c.node_id,
                receipt_id=receipt["id"],
            )
            # upsert increments activation_count, updates last_seen_at and last_receipt_id

    receipt["terrace_reached"] = terrace  # max=1 for lithos_retrieve; LLM synthesis is lithos-enrich
    receipt["final_nodes"] = summarize_reasons(final_nodes)

    write_receipt(receipt)

    # Project Candidates to ResultItems (compatible with lithos_search)
    result_items = [
        project_to_result_item(c, knowledge_manager)
        for c in final_nodes
    ]

    return RetrievalResult(
        results=result_items,
        temperature=temp,
        terrace_reached=terrace,
        receipt_id=receipt["id"]
    )
```

### `should_enrich_with_llm` policy (lithos-enrich, v2+)

This policy governs when `lithos-enrich` should trigger background LLM synthesis for a node or cluster. It runs asynchronously and is **not part of the retrieval pipeline**.

```python
def should_enrich_with_llm(node_id, temp, conflict_edges) -> bool:
    if conflict_edges:
        return True
    if temp > 0.6:
        return True
    return False
```

Note: **low temperature does not automatically trigger LLM** — synthesis is reserved for high-uncertainty or conflict scenarios.

---

## 5.5 Fast Re-rank (Terrace 1)

```python
def rerank_fast(q: QueryContext, pool: list[Candidate]) -> list[Candidate]:
    out = []
    for c in pool:
        stats = get_node_stats(c.node_id)
        meta  = read_frontmatter(c.node_id)

        type_prior = note_type_prior(meta.note_type)
        scope_prior = namespace_affinity(meta.namespace, q.namespace_filter)
        concept_damp = concept_penalty_if_overused(meta.note_type, c.node_id, q)

        decay_boost = spaced_repetition_boost(stats)
        ignore_pen  = ignored_penalty(stats)
        mislead_pen = misleading_penalty(stats)

        # Graph affinity: how connected is this node to other high candidates?
        graph_aff = quick_graph_affinity(c.node_id, [x.node_id for x in pool[:20]], meta.namespace)

        c.score = (
            c.score
            + 0.25 * type_prior
            + 0.15 * scope_prior
            + 0.15 * graph_aff
            + 0.10 * decay_boost
            - 0.20 * ignore_pen
            - 0.30 * mislead_pen
            - 0.15 * concept_damp
        )

        # Keep reasons for auditor
        c.reasons += build_reason_fragments(type_prior, scope_prior, graph_aff, ignore_pen, concept_damp)

        out.append(c)

    # Diversity: MMR-style removal of near-duplicates
    return mmr_diversify(out, lambda a,b: similarity(a.node_id, b.node_id), top_n=200)
```

**MVP 1 implementation note.** The actual MVP 1 `_rerank_fast` in `src/lithos/lcma/retrieve.py` uses a slightly different weighted combination: scout-name-keyed weights pulled from `LcmaConfig.rerank_weights` (e.g. `vector: 0.35`, `lexical: 0.25`) rather than prior-name-keyed weights (`type_prior`, `scope`, `graph`, ...). It applies a greedy MMR pass over the top 30 candidates using title-token Jaccard as the similarity metric — no embedding lookups in the hot path. The prior-name formula above captures the MVP 2+ target; differentiated priors and graph/salience features from `stats.db` are layered in as reinforcement data becomes available.

### `note_type_prior()` definition

Lookup returning a retrieval prior weight per `note_type`. Configurable via `LcmaConfig.note_type_priors`.

**MVP 1: all priors are neutral (0.5).** Hand-tuned priors are brittle before learning signals exist to validate them. Let reinforcement differentiate note types over time. Differentiated priors may be introduced in MVP 2+ once there is data to inform them.

```python
NOTE_TYPE_PRIORS: dict[str, float] = {
    "observation": 0.5,
    "agent_finding": 0.5,
    "summary": 0.5,
    "concept": 0.5,
    "task_record": 0.5,
    "hypothesis": 0.5,
}

def note_type_prior(note_type: str) -> float:
    return NOTE_TYPE_PRIORS.get(note_type, 0.5)  # default 0.5 for unknown types
```

---

## 5.6 Learning Updates After a Task (Bidirectional)

Called after an agent produces output.

Inputs:

- retrieval result (final_nodes + receipt)
- output text
- optional user feedback / acceptance
- agent’s explicit citations (best if you can enforce)

**Call site**: `post_task_update()` is invoked server-side when an agent calls `lithos_task_complete` with the optional feedback params introduced in MVP 2: `cited_nodes: list[str]` and `misleading_nodes: list[str]`. The server retrieves the last retrieval receipt for that `task_id` from the `receipts` table, diffs `cited_nodes` against `final_nodes` to determine used vs. ignored, and calls `post_task_update()`. No new MCP tool is needed — `lithos_task_complete` is the natural extension point since agents already call it at task end.

```python
def post_task_update(q: QueryContext, retrieval: RetrievalResult, output_text: str, citations: list[str], user_feedback=None):
    # ---- Determine "used" vs "ignored" vs "misleading" ----
    used = set()
    ignored = set()
    misleading = set()

    for c in retrieval.final_nodes:
        if c.node_id in citations:
            used.add(c.node_id)
            continue

        # heuristic: output overlaps / embedding similarity to note
        if output_supports_node(output_text, c.node_id):
            used.add(c.node_id)
        else:
            ignored.add(c.node_id)

    # user feedback can mark misleading explicitly
    if user_feedback and user_feedback.get("misleading_nodes"):
        misleading |= set(user_feedback["misleading_nodes"])
        ignored -= misleading

    # ---- Positive reinforcement ----
    reinforce_nodes(used, q)
    reinforce_edges_between(used, q)

    # ---- Negative reinforcement ----
    penalize_ignored(ignored, q)
    penalize_misleading(misleading, q)

    # ---- Update coactivation counts (for later concept formation) ----
    update_coactivation(retrieval.final_nodes, q)

    # ---- Hypothesis lifecycle hooks ----
    update_hypotheses(used, misleading, q)

    # ---- Contradiction workflow hooks ----
    update_conflict_states_if_needed(used, q)
```

### Node/edge updates

```python
def reinforce_nodes(node_ids: set[str], q: QueryContext):
    for nid in node_ids:
        stats = get_node_stats(nid)
        stats.retrieval_count += 1
        stats.last_used_at = now()
        stats.salience = clamp(stats.salience + 0.02, 0.0, 1.0)
        stats.spaced_rep_strength = min(1.0, stats.spaced_rep_strength + 0.05)
        write_node_stats(nid, stats)

def reinforce_edges_between(node_ids: set[str], q: QueryContext):
    pairs = all_pairs(list(node_ids))
    for a,b in pairs:
        e = get_or_create_edge(a,b,type="related_to",namespace=effective_namespace(q))
        e.weight = clamp(e.weight + 0.03, 0.0, 1.0)
        e.updated_at = now()
        write_edge(e)
```

### Negative reinforcement

```python
def penalize_ignored(node_ids: set[str], q: QueryContext):
    for nid in node_ids:
        stats = get_node_stats(nid)
        stats.ignored_count += 1
        # mild salience decay if chronic
        if stats.ignored_count > 5 and stats.ignored_count > stats.retrieval_count:
            stats.salience = clamp(stats.salience - 0.02, 0.0, 1.0)
        write_node_stats(nid, stats)

def penalize_misleading(node_ids: set[str], q: QueryContext):
    for nid in node_ids:
        stats = get_node_stats(nid)
        stats.misleading_count += 1
        stats.salience = clamp(stats.salience - 0.05, 0.0, 1.0)
        # optional: quarantine if repeatedly misleading
        if stats.misleading_count >= 3:
            set_note_status(nid, "quarantined")
        write_node_stats(nid, stats)

def weaken_edges_for_bad_context(retrieved_nodes: list[str], bad_nodes: set[str], q: QueryContext):
    # If certain nodes were bad, weaken edges that pulled them in.
    for bad in bad_nodes:
        neighbors = top_incoming_edges(bad, namespace=effective_namespace(q), limit=10)
        for e in neighbors:
            e.weight = clamp(e.weight - 0.05, 0.0, 1.0)
            write_edge(e)
```

---

## 5.7 Consolidation (WM → LTM “rest period”)

> **Note**: This function runs inside `lithos-enrich`, not in the `lithos_retrieve` hot path. It is triggered via the enrichment queue: `lithos_task_complete` emits an event that writes a `task_complete` entry to `enrich_queue`; the next drain cycle calls `consolidate()` for that task. For the daily full sweep, consolidation runs for all tasks completed since the last full run.

Consolidation hooks into the existing task lifecycle. A natural trigger is when `lithos_task_complete` is called — the coordination system already tracks `task_id` and `agent` for each task.

Run:

- at task boundaries (triggered by `lithos_task_complete`)
- on schedule
- or when WM exceeds size threshold

```python
def consolidate(task_id: str, agent_id: str):
    # WM = nodes retrieved/used during this task (from working_memory table in stats.db)
    wm_nodes = get_working_memory(task_id, agent_id)
    if not wm_nodes:
        return

    # Reinforce edges among frequently co-activated items
    frequent = [n for n in wm_nodes if wm_activation_count(n) >= 2]
    reinforce_edges_between(set(frequent), QueryContext(...))

    # Update summaries (only for summary/concept types, and only if changed materially)
    update_summaries(frequent)

    # Promote hypotheses that were used successfully and not contradicted
    promote_confirmed_hypotheses(frequent)

    # Concept node maintenance (cluster detection)
    maybe_update_concepts(namespace_of_task(task_id))
```

---

## 5.8 Concept Formation + Damping

> **Note**: This function runs inside `lithos-enrich`, not in the `lithos_retrieve` hot path.

Concepts emerge in two phases: derived clusters first, then explicit promotion to notes (see §1.1 Concept Node).

```python
def maybe_update_concepts(namespace: str):
    # Phase 1: detect and track clusters in stats.db (no note materialization)
    clusters = detect_stable_clusters(namespace, min_size=5, min_coactivation=3)
    upsert_derived_clusters(clusters, namespace)  # persist in stats.db

    # Phase 2: promote stable clusters to concept notes (explicit, not automatic)
    # Only promote clusters that exceed a stability threshold (e.g., seen in N+ drain cycles)
    for cluster in clusters:
        if not cluster_exceeds_promotion_threshold(cluster):
            continue
        # find_or_create uses lithos_write with note_type="concept"
        concept_id = find_or_create_concept_node(cluster, namespace)
        # link via edges.db type="is_example_of"
        link_concept_to_members(concept_id, cluster)

        # Damping: cap concept salience and avoid repeated dominance
        stats = get_node_stats(concept_id)
        stats.salience = min(stats.salience, 0.85)
        write_node_stats(concept_id, stats)

def concept_penalty_if_overused(note_type: str, node_id: str, q: QueryContext) -> float:
    if note_type != "concept":
        return 0.0
    recent = count_recent_retrievals(node_id, window_hours=24, namespace=effective_namespace(q))
    return min(0.4, recent * 0.05)   # increasing penalty for repeated concept retrieval
```

---

## 5.9 Contradiction Workflow

When conflicts detected:

```python
def handle_contradiction(a_id: str, b_id: str, evidence: dict, namespace: str):
    e = get_or_create_edge(a_id, b_id, type="contradicts", namespace=namespace)
    e.weight = max(e.weight, 0.6)
    e.conflict_state = e.conflict_state or "unreviewed"
    e.evidence = merge_evidence(e.evidence, evidence)
    write_edge(e)

def surface_conflicts(nodes: list[str], q: QueryContext) -> list[str]:
    if not q.surface_conflicts:
        return []
    conflicts = []
    for nid in nodes:
        conflicts += get_active_contradictions(nid, namespace=effective_namespace(q))
    return unique(conflicts)
```

Resolution is an explicit action (agent or user):

```python
def resolve_conflict(edge_id: str, resolution: str, resolver: str):
    # resolution in {"accepted_dual","superseded","refuted","merged"}
    e = read_edge(edge_id)
    e.conflict_state = resolution
    e.provenance_actor = resolver
    e.updated_at = now()
    write_edge(e)
```

---

## 5.10 Embedding Space Versioning / Migration

Lithos currently uses a single ChromaDB collection `"knowledge"` with `all-MiniLM-L6-v2`. LCMA adds versioning via separate ChromaDB collections per embedding space.

```python
def embed_node(node_id: str, embedding_space_id: str):
    text = read_note_text(node_id)
    # Use the model associated with this space
    vec = embedding_model(embedding_space_id).embed(text)
    # Store in ChromaDB collection "knowledge_{embedding_space_id}"
    collection = chroma_client.get_or_create_collection(
        f"knowledge_{embedding_space_id}",
        metadata={"hnsw:space": "cosine"}
    )
    # Chunk and store as per existing ChromaIndex.add_document() pattern
    store_chunks_in_collection(node_id, text, vec, collection)

def migrate_embeddings(new_space: str, strategy: str):
    # strategy: "batch" | "lazy"
    if strategy == "batch":
        for node_id in all_nodes():
            embed_node(node_id, new_space)
    elif strategy == "lazy":
        set_default_embedding_space(new_space)
        # embed on edit or on retrieval touch
```

Retrieval queries multiple ChromaDB collections during transition:

```python
def scout_vector(q: QueryContext, k: int) -> list[Candidate]:
    spaces = active_embedding_spaces()  # e.g., ["emb_v2", "emb_v1"]
    results = []
    for space in spaces:
        collection = chroma_client.get_collection(f"knowledge_{space}")
        results += collection.query(query_texts=[q.query_text], n_results=k//len(spaces))
    return normalize_and_deduplicate(results)  # deduplicate by doc_id as existing code does
```

---

## 5.11 Schema Versioning for Notes

Each note has `schema_version` (default: 1 for existing notes without it).

**MVP 1: lazy defaults + write-back-on-touch.** Since unknown frontmatter fields already survive round-trips (tested in `test_integration_conformance.py`), LCMA fields are additive and need no eager migration. When a note is read, missing LCMA fields get in-memory defaults. When a note is next written via `lithos_write`, the defaults are persisted. This avoids unnecessary file churn and respects the "existing notes without LCMA fields remain valid" guarantee.

```python
def read_with_lcma_defaults(note_path: str) -> tuple[dict, str]:
    meta, body = read_frontmatter(note_path)    # existing python-frontmatter parsing
    # Apply LCMA defaults at read time — no file write needed
    meta.setdefault("schema_version", 1)
    meta.setdefault("namespace", derive_namespace_from_path(note_path))
    meta.setdefault("access_scope", "shared")
    meta.setdefault("note_type", "observation")
    meta.setdefault("status", "active")
    return meta, body
```

**MVP 2: migration registry.** A formal migration registry (`data/.lithos/migrations/registry.json`) is introduced only when semantic schema changes (not just additive fields) require it:

```json
{
  "current_version": 2,
  "migrations": [
    {"from": 1, "to": 2, "name": "add_namespace_access_scope_note_type"}
  ]
}
```

Migrations must be idempotent and never remove existing fields.

---

## 5.12 `lithos-enrich` Worker Pseudocode

> **Note**: This is the other half of the two-component architecture. `lithos-enrich` runs as an in-process background worker inside the Lithos server, performing expensive query-independent enrichment (edge building, salience scoring, decay, and — in MVP 3 — concept formation and LLM synthesis). Ships in MVP 2; schema and queue created in MVP 1.

```python
class EnrichWorker:
    def __init__(self, config: LcmaConfig, event_bus: EventBus, db: StatsDB):
        self.config = config
        self.event_bus = event_bus
        self.db = db
        self._running = False
        self._drain_task: asyncio.Task | None = None
        self._sweep_task: asyncio.Task | None = None

    async def start(self):
        self._running = True
        # Subscribe to knowledge-mutating events via queue-based EventBus API.
        # EventBus.subscribe() returns an asyncio.Queue filtered by event_types.
        # Event type constants use dot-delimited names (events.py):
        #   note.created, note.updated, note.deleted,
        #   task.completed, task.cancelled, finding.posted
        self._event_queue = self.event_bus.subscribe(
            event_types=[
                "note.created", "note.updated", "note.deleted",
                "task.completed",
                "finding.posted",
                "edge.upserted",  # new in MVP 1
            ]
        )
        # Start event consumer + periodic loops
        self._consumer_task = asyncio.create_task(self._consume_events())
        self._drain_task = asyncio.create_task(self._drain_loop())
        self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def stop(self):
        self._running = False
        if self._consumer_task:
            self._consumer_task.cancel()
        if self._drain_task:
            self._drain_task.cancel()
        if self._sweep_task:
            self._sweep_task.cancel()
        if self._event_queue:
            self.event_bus.unsubscribe(self._event_queue)

    # ---- Event consumer: read from queue, enqueue work ----

    async def _consume_events(self):
        """Drain the EventBus subscription queue into enrich_queue rows."""
        while self._running:
            event = await self._event_queue.get()
            if event.type == "task.completed":
                self._enqueue_task_work(event)
            elif event.type == "edge.upserted":
                self._enqueue_edge_work(event)
            else:
                self._enqueue_node_work(event)

    def _enqueue_node_work(self, event):
        """Enqueue node-level enrichment work."""
        self.db.execute(
            "INSERT INTO enrich_queue (trigger_type, node_id, triggered_at) VALUES (?, ?, ?)",
            (event.type, event.data.get("doc_id"), now_iso())
        )

    def _enqueue_task_work(self, event):
        """Enqueue task-level consolidation work."""
        self.db.execute(
            "INSERT INTO enrich_queue (trigger_type, task_id, triggered_at) VALUES (?, ?, ?)",
            ("task.completed", event.data.get("task_id"), now_iso())
        )

    def _enqueue_edge_work(self, event):
        """Enqueue enrichment for both nodes connected by an edge."""
        for node_id in (event.data.get("from_id"), event.data.get("to_id")):
            if node_id:
                self.db.execute(
                    "INSERT INTO enrich_queue (trigger_type, node_id, triggered_at) VALUES (?, ?, ?)",
                    ("edge.upserted", node_id, now_iso())
                )

    # ---- Drain loop: process pending queue entries ----

    async def _drain_loop(self):
        while self._running:
            await asyncio.sleep(self.config.enrich_drain_interval_minutes * 60)
            await self.drain()

    async def drain(self):
        """Process pending enrich_queue entries, deduplicating by work type."""
        # Node-level work: deduplicate by node_id
        node_rows = self.db.query(
            "SELECT DISTINCT node_id FROM enrich_queue "
            "WHERE node_id IS NOT NULL AND processed_at IS NULL"
        )
        for row in node_rows:
            await self._enrich_node(row.node_id)
            self.db.execute(
                "UPDATE enrich_queue SET processed_at = ? "
                "WHERE node_id = ? AND processed_at IS NULL",
                (now_iso(), row.node_id)
            )

        # Task-level work: deduplicate by task_id
        task_rows = self.db.query(
            "SELECT DISTINCT task_id FROM enrich_queue "
            "WHERE node_id IS NULL AND processed_at IS NULL"
        )
        for row in task_rows:
            await self._consolidate_task(row.task_id)
            self.db.execute(
                "UPDATE enrich_queue SET processed_at = ? "
                "WHERE task_id = ? AND node_id IS NULL AND processed_at IS NULL",
                (now_iso(), row.task_id)
            )

    # ---- Full sweep: authoritative repair pass ----

    async def _sweep_loop(self):
        while self._running:
            await asyncio.sleep(self._seconds_until_next_sweep())
            await self.full_sweep()

    async def full_sweep(self):
        """Daily authoritative pass: recompute decay, concept clusters, WM eviction."""
        all_nodes = get_all_node_ids()
        for node_id in all_nodes:
            apply_decay(node_id)                     # salience decay based on last_used_at
        maybe_update_concepts(namespace=None)         # full concept cluster analysis
        evict_stale_working_memory(                   # WM eviction policy (§4.4)
            ttl_days=self.config.wm_eviction_days
        )

    # ---- Dispatch helpers ----

    async def _enrich_node(self, node_id: str):
        """Run enrichment for a single node: update stats, rebuild edges if needed."""
        update_node_salience(node_id)
        rebuild_projected_edges(node_id)              # re-sync derived_from edges

    async def _consolidate_task(self, task_id: str):
        """Run WM→LTM consolidation for a completed task."""
        agent_id = get_task_agent(task_id)
        consolidate(task_id, agent_id)                # §5.7 consolidation logic

    def _seconds_until_next_sweep(self) -> float:
        """Compute seconds until next scheduled sweep (default: daily at 3am)."""
        # Parse self.config.enrich_full_sweep_cron
        ...
```

Error handling: enrichment failures for individual nodes/tasks are logged and skipped — the next drain cycle or full sweep will retry. No dead-letter queue in MVP 2; add if failure rates warrant it.

---

## 5.13 Prerequisites (must be resolved before MVP 1 implementation)

### Async search hot-path

`lithos_retrieve` orchestrates multiple scouts in parallel, each wrapping existing sync search backends. Currently:

- `SearchManager.full_text_search()` is sync ([search.py:870](src/lithos/search.py#L870)) — the server already has a TODO noting it should not block the event loop ([server.py:1181](src/lithos/server.py#L1181))
- `SearchManager.semantic_search()` is sync ([search.py:910](src/lithos/search.py#L910)) — the embedding model load uses `asyncio.to_thread` but the search call itself does not
- `SearchManager.hybrid_search()` is sync ([search.py:962](src/lithos/search.py#L962)) — calls both of the above sequentially

Before LCMA adds 7+ scouts calling into these backends, the sync search methods must be wrapped in `asyncio.to_thread()` or equivalent. Otherwise `lithos_retrieve` will block the event loop for the entire multi-scout pipeline, starving other async operations (event bus delivery, MCP message handling, enrich worker drains).

**Resolution**: Wrap `full_text_search()`, `semantic_search()`, and `hybrid_search()` calls in `asyncio.to_thread()` at the call site (in `lithos_search` tool handler and in each LCMA scout). This is a small change to the server/scout layer and does not require modifying the `SearchManager` class itself.

### Score normalization

See §5.3.1 — `merge_and_normalize` with per-scout min-max normalization must be implemented before `lithos_retrieve` ships.

---

# 6) MVP Roadmap (so this doesn’t explode)

Each MVP builds on the existing Lithos infrastructure. Existing tools are extended with backward-compatible optional parameters (e.g., `lithos_write` gains optional `note_type`, `namespace`, `access_scope` params with defaults that preserve current behavior). LCMA adopts the shared `lithos_write` status-based response contract from the source-url dedup + digest v2 plans (`created`/`updated`/`duplicate` with optional `warnings`) rather than introducing a separate return shape.

Write-contract note for LCMA params:

- New LCMA fields follow the existing write-contract model: omitted values preserve existing values on update unless a field-specific clear rule is explicitly defined.
- Any field that needs clear semantics at the MCP boundary must specify them individually before implementation.

## MVP 1 (7 scouts + Terrace 1 — wraps existing engines)

> **Note**: `lithos_retrieve` in MVP 1 implements Terrace 0 + Terrace 1 only. Background LLM synthesis belongs to `lithos-enrich` (MVP 3).

- Verify existing `coordination.db` migration path remains stable (already implemented)
- `lithos_retrieve` tool orchestrating scouts internally
- Scouts: vector (ChromaDB), lexical (Tantivy), exact/alias/path (KnowledgeGraph), tags/recency (KnowledgeManager), provenance (`derived_from_ids`), task-context (coordination.db, when `task_id` provided), freshness (`expires_at`/`is_stale`)
- Basic rerank with `note_type` priors (requires new optional frontmatter fields)
- Extend `lithos_write` with optional LCMA frontmatter params (`note_type`, `namespace`, `access_scope`, etc.) while reusing the shared cross-plan write payload (`source_url`, `derived_from_ids`, `ttl_hours`/`expires_at`, etc.) and shared status-based response envelope
- Retrieval receipts written to `receipts` table in `stats.db`
- Temperature returned as fixed default (0.5) — computed temperature activates in MVP 3 when edges exist
- `data/.lithos/edges.db` (related_to) + basic reinforcement
- `data/.lithos/stats.db` (node_stats, coactivation)
- New tools: `lithos_edge_upsert`, `lithos_edge_list`
- Schema versioning: `schema_version` field (lazy defaults + write-back-on-touch; migration registry deferred to MVP 2)
- `summaries` stored as nested YAML (`summaries: { short, long }`)
- `lithos_retrieve` owns namespace/access-scope enforcement in MVP 1; legacy tools remain unchanged

## MVP 2 (v2 essentials)

> **Note**: `lithos-enrich` is introduced in MVP 2 as an in-process background worker. It handles edge building, salience scoring, and decay. `lithos_retrieve` continues to use only Terrace 0 + Terrace 1, now benefiting from pre-computed enrichment. Concept formation and LLM synthesis are deferred to MVP 3.

- Negative reinforcement on ignored/misleading (stats.db updates)
- Contradiction edges with `unreviewed` state + `lithos_conflict_resolve` tool
- `lithos_node_stats` tool — view salience and usage stats from `stats.db`
- Schema migration registry (`data/.lithos/migrations/registry.json`) — deferred from MVP 1
- Namespace + access_scope filtering on scouts
- Consolidation hook on `lithos_task_complete`
- New scouts: graph (NetworkX + edges.db), coactivation/bridge (stats.db), source-url/domain (`_source_url_to_id`)
- Extend `lithos_task_complete` with optional feedback params: `cited_nodes: list[str]`, `misleading_nodes: list[str]` — server calls `post_task_update()` on receipt
- `lithos-enrich` auto-extracts `entities` from notes (deferred from MVP 1)
- `lithos-enrich` pseudocode finalized (§5.12) before implementation begins
- Differentiated `note_type_priors` tuning based on MVP 1 learning data

## MVP 3 (Hofstadter-flavored)

> **Note**: Background LLM synthesis is part of `lithos-enrich`, not `lithos_retrieve`. It runs asynchronously and produces persistent artifacts. `lithos_retrieve` remains Terrace 0 + Terrace 1 only.

- Analogy scout (frame extraction — new, no existing equivalent)
- Exploration scout (novelty/random/mixed modes, temperature-guided)
- Temperature-based exploration depth
- Concept nodes (regular notes with `note_type: "concept"`) + damping
- Embedding space versioning via ChromaDB collections

---

# 7) Implementation Notes for Lithos Specifically

- Keep Markdown as source of truth for _content_ and stable metadata.
- Keep SQLite stores as truth for _dynamic signals_ (weights, stats, receipts — all in `stats.db`).
- Make every agent write action policy-gated:
    - “agent can propose; librarian/auditor confirms” if you want strictness
    - Can leverage existing claim mechanics (`lithos_task_claim`) for write gating
- Treat **retrieval receipts** as a key product feature:
    - debugging, trust, and future auto-tuning all depend on them.

## Alignment with Existing Lithos (preserving backward compatibility)

### Existing tools preserved (post-consolidation)

- Knowledge: `lithos_write`, `lithos_read`, `lithos_delete`, `lithos_search`, `lithos_list`, `lithos_cache_lookup`
- Graph: `lithos_tags`, `lithos_related` (the composite tool that superseded the pre-1.0 `lithos_links` and `lithos_provenance`)
- Agents: `lithos_agent_register`, `lithos_agent_info`, `lithos_agent_list`
- Coordination: `lithos_task_create`, `lithos_task_update`, `lithos_task_claim`, `lithos_task_renew`, `lithos_task_release`, `lithos_task_complete`, `lithos_task_cancel`, `lithos_task_list`, `lithos_task_status`
- Findings: `lithos_finding_post`, `lithos_finding_list`
- System: `lithos_stats`

### New LCMA tools (additive only)

**MVP 1:**

- `lithos_retrieve` — PTS-based retrieval orchestrating scouts
- `lithos_edge_upsert` — create or update a typed edge
- `lithos_edge_list` — query edges by node, type, or namespace

**MVP 2:**

- `lithos_node_stats` — view/query salience and usage stats
- `lithos_conflict_resolve` — contradiction resolution

**MVP 3:**

- `lithos_receipts` — query retrieval audit history

  ```python
  lithos_receipts(
      agent: str | None = None,
      task_id: str | None = None,
      since: str | None = None,   # ISO datetime
      limit: int = 50
  ) -> {
      "receipts": [{ "ts", "query", "agent", "task_id", "temperature",
                      "terrace_reached", "final_nodes", "conflicts_surfaced" }]
  }
  ```

  Reads from the `receipts` table in `stats.db` with optional filtering. Read-only query tool; does not affect retrieval behavior or stats.

Provenance query policy:

- Canonical lineage queries use the `provenance` section of `lithos_related` (frontmatter/index based).
- `lithos_edge_list` is for typed LCMA edges and projected relationships; it is not the canonical lineage API.

### Existing frontmatter fields preserved

All existing `KnowledgeMetadata` fields (`id`, `title`, `author`, `created_at`, `updated_at`, `tags`, `aliases`, `confidence`, `contributors`, `source`, `supersedes`) are kept unchanged. LCMA adds optional fields with defaults.

LCMA is also compatible with cross-plan metadata additions: `source_url`, `derived_from_ids`, and `expires_at`.

### Existing infrastructure preserved

| Component | Location | Role in LCMA |
| --- | --- | --- |
| Tantivy | `data/.tantivy/` | Lexical scout backend |
| ChromaDB | `data/.chroma/` | Vector scout backend, extended with collection-per-space versioning |
| NetworkX | `data/.graph/` | Wiki-link graph scout (alongside new edges.db) |
| coordination.db | `data/.lithos/coordination.db` | Agent registry, task lifecycle, WM integration point |

### Key design decisions

- **`confidence` vs `salience`**: `confidence` (frontmatter) = author's belief about accuracy. `salience` (stats.db) = retrieval utility learned from usage. Both are 0–1 floats but serve different purposes.
- **NetworkX vs edges.db**: NetworkX handles structural `[[wiki-link]]` navigation and powers the `links` section of `lithos_related`. edges.db handles semantic/learned relationships with weights and types. Both are queried by the graph scout.
- **Declared provenance vs learned edges**: `derived_from_ids` is the source of truth for declared lineage. `edges.db` can carry mirrored `derived_from` edges as an accelerator only.
- **Frontmatter vs stats.db**: Static metadata in frontmatter (author, tags, note_type). Dynamic signals in stats.db (salience, retrieval_count, decay). This avoids constant file rewrites from learning updates.
- **Concept nodes**: Emerge as derived clusters in `stats.db` first, then promoted to regular `KnowledgeDocument` notes with `note_type: “concept”` via `lithos_write` once stable. Not auto-materialized — promotion requires exceeding a stability threshold to avoid noisy git churn.

### LLM integration scope

Background LLM synthesis (the `should_enrich_with_llm()` policy and `llm_interpretive_synthesize()` worker in §5.4/§5.12) requires an external LLM provider and is not available in MVP 1 or MVP 2. `lithos_retrieve` always terminates at Terrace 1 — no local model is bundled and no external LLM call is made. LLM synthesis ships in MVP 3 as part of `lithos-enrich`, configured via `LithosConfig.lcma.llm_provider`.

### 7.z `LcmaConfig` Schema (Draft)

```python
class LcmaConfig(BaseModel):
    """LCMA configuration subtree under LithosConfig.lcma"""
    enabled: bool = False                          # feature gate for MVP 1
    enrich_drain_interval_minutes: int = 5
    enrich_full_sweep_cron: str = "0 3 * * *"      # daily at 3am
    rerank_weights: dict[str, float] = {
        "type_prior": 0.25, "scope": 0.15, "graph": 0.15,
        "decay": 0.10, "ignore": -0.20, "mislead": -0.30,
        "concept_damp": -0.15,
    }
    note_type_priors: dict[str, float] = {
        "observation": 0.5, "agent_finding": 0.5, "summary": 0.5,
        "concept": 0.5, "task_record": 0.5, "hypothesis": 0.5,
    }  # All neutral in MVP 1; differentiate in MVP 2+ once learning data exists
    temperature_default: float = 0.5               # used when edges < threshold
    temperature_edge_threshold: int = 50            # min edges for computed temp
    wm_eviction_days: int = 7
    llm_provider: str | None = None                # MVP 3+, background LLM synthesis
```

Ship with hardcoded defaults in MVP 1. Config override via `LithosConfig.lcma` available from first release but only becomes load-bearing when `lithos-enrich` ships in MVP 2.

### 7.x Two-Component Design Rationale

- **`lithos-enrich`** does the expensive, query-independent work in the background. Its outputs (concept nodes as `.md` files, edges in `edges.db`, salience scores in `stats.db`) are persistent and discoverable via `lithos_search`.
- **`lithos_retrieve`** is the thin query-time layer that assembles pre-computed enrichment into a ranked result for a specific query. It is fast because the heavy lifting already happened.
- Clients who only need content can use `lithos_search` and benefit from enrichment passively — concept nodes created by `lithos-enrich` are regular `.md` files indexed by the standard search pipeline.
- Clients who need salience-weighted, graph-aware, multi-scout ranked results use `lithos_retrieve`.
- **Background LLM synthesis belongs to `lithos-enrich`** (not the retrieval pipeline) because: (a) it is expensive, (b) it is query-independent — it synthesizes knowledge proactively, producing persistent artifacts (summaries, concept notes, edge annotations), (c) clients should not block waiting for LLM synthesis. It is deliberately **not** numbered as "Terrace 2" to avoid implying it is a step in the retrieval pipeline.
- **Response compatibility**: `lithos_retrieve` returns a `results` list with the same fields as `lithos_search` (`id`, `title`, `snippet`, `score`, `path`, `source_url`, `updated_at`, `is_stale`, `derived_from_ids`). LCMA-specific fields (`reasons`, `scouts`, `salience`) are additive. The envelope adds `temperature`, `terrace_reached`, and `receipt_id`. Clients that only read `results[].id` or `results[].score` work identically with both tools.
- **Enrichment queue pattern**: Rather than triggering `lithos-enrich` directly on each action, triggering actions emit events via the existing Lithos event bus. The in-process `lithos-enrich` worker subscribes to these events and writes to an `enrich_queue` table in `stats.db`. A periodic drain processes pending work, deduplicating multiple events for the same node. A daily full sweep catches anything missed and recomputes global signals (decay, concept clusters). This avoids redundant enrichment runs (e.g., 10 task completions → 1 enrichment run), enables incremental targeted processing, and leverages existing event infrastructure with no new event system required.

### 7.y When to Use Which Tool

`lithos_search` and `lithos_retrieve` have distinct, complementary roles. `lithos_retrieve` is additive, not a replacement — `lithos_search` is not deprecated.

- **`lithos_search`** is the canonical **low-level search API** — the stable, direct-access retrieval primitive.
  - Use for: exact lookup, keyword/fulltext search, semantic search without extra orchestration, UI/debugging/manual inspection, low-latency or deterministic behavior.
  - Behavior: fast, simple, predictable, low-policy. Returns results from a single search mode (fulltext, semantic, or hybrid RRF).

- **`lithos_retrieve`** is the canonical **high-level retrieval API** — the LCMA retrieval primitive for task-oriented context assembly.
  - Use for: "give me the best context for this task", multi-document synthesis, design/debug/decision workflows, graph-aware or salience-aware retrieval, contradiction surfacing and exploration behavior.
  - Behavior: orchestrated, salience-weighted, multi-scout, graph-aware. Uses pre-computed enrichment from `lithos-enrich`.

Both tool MCP descriptions should make this distinction explicit so agents can choose appropriately.

---

## 8) Open Implementation Questions

The following items were identified during design review and need resolution during implementation. They are ordered roughly by the section they affect.

1. **~~Edge tool boundary (§4.3)~~**: **Resolved** — merged `lithos_edge_create` + `lithos_edge_update` into a single `lithos_edge_upsert`. Creates if absent, updates if exists. `lithos_conflict_resolve` deferred to MVP 2.

2. **~~Namespace derivation algorithm (§1.1)~~**: **Resolved** — strip `knowledge/` prefix, take directory path, fall back to `"default"` for root-level notes. Full rule with examples in §1.1 implementation notes.

3. **`enrich_queue` task-level dedup (§4.4)**: Current dedup is `SELECT DISTINCT node_id WHERE processed_at IS NULL`, but `task_complete` triggers are task-level (`node_id` is NULL). Add `task_id` to the dedup key for task-level triggers. Drain should handle node-level and task-level work separately. **Resolved**: Drain handles node-level and task-level work separately (see updated §4.4 dedup description).

4. **~~Schema migration timing (§5.11)~~**: **Resolved** — MVP 1 uses lazy defaults + write-back-on-touch. Migration registry deferred to MVP 2 for semantic changes only.

5. **~~`receipts.jsonl` scalability (§4.6)~~**: **Resolved** — receipts are now stored in the `receipts` table in `stats.db` with indexed columns `(ts)`, `(task_id)`, `(agent_id)`, consistent with other SQLite stores. **Resolved**: Receipts moved to `receipts` table in `stats.db`. All JSONL references replaced.

6. **Cold-start temperature guard (§5.3)**: On cold start with no edges, coherence ≈ 0 and temperature ≈ 1.0, triggering maximum exploration on every query. Add a guard: if `total_edges < threshold`, use a fixed default temperature (e.g., 0.5) instead of computing from edge coherence.

7. **Coactivation scope (§5.6)**: `update_coactivation(retrieval.final_nodes, q)` creates O(N²) pairs. With 30 final nodes that's 435 pairs per retrieval. Scope to top-k used/cited nodes (e.g., top 10) rather than all final nodes.

8. **Rerank weight configurability (§5.5)**: The Terrace 1 linear combination uses hardcoded coefficients (0.25, 0.15, etc.). Move these into `LithosConfig.lcma` as a configurable weights dict to enable tuning without code changes.

9. **Summaries ownership (§4.2)**: Clarify whether `summaries: {short, long}` is agent-written (via `lithos_write`), auto-generated (by `lithos-enrich`), or both. If enrich-generated, this implies background frontmatter writes — define precedence rules (does agent-written take priority?). **Resolved**: `summaries` is agent-written only in MVP 1 (via `lithos_write`). In MVP 2, `lithos-enrich` may generate summaries for notes where `summaries` is empty or `note_type` is `concept`/`summary`. Agent-written summaries take precedence — enrich never overwrites non-empty agent-written values.

10. **Enrich subscriber queue sizing (§3.2)**: The event bus is lossy (default subscriber queue size: 100, drops silently when full). Size the `lithos-enrich` subscriber queue much larger (e.g., 10,000) or have the enrich worker drain its asyncio.Queue frequently (seconds, not minutes) to minimize double-buffering overflow risk.
