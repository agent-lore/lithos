# LCMA Design Doc

## 0. Summary

LCMA turns Lithos from “notes + embeddings” into a **cognitive substrate**:

- **Parallel Terraced Scan (PTS)** retrieval: many cheap probes → selective deepening
- **Typed, weighted graph edges** that strengthen _and weaken_ over time
- **Working Memory (WM)** and **Long-Term Memory (LTM)** split
- **Concept nodes** that emerge from stable clusters (with damping to avoid domination)
- **Multi-agent coordination** via namespaces, scopes, and task-shared WM
- **Auditable retrieval** (“why was this retrieved?” receipts)

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
- `access_scope` (enum: `agent_private|task|project|shared|user_private`, default `shared`)
- `note_type` (enum: `observation|agent_finding|summary|concept|task_record|hypothesis`, default `observation`)
- `entities` (list of extracted entity names)
- `status` (enum: `active|archived|quarantined`, default `active`)
- `summaries.short` / `summaries.long` (optional)

**Dynamic signals (stored in `stats.db`, NOT in frontmatter):**

- `salience` (float, default 0.5 — retrieval utility, complementary to `confidence`)
- `usage_stats` (retrieval_count, last_used, coactivation counts)
- embeddings managed by ChromaDB (see §4.5)

### Edge

LCMA introduces typed, weighted edges stored in `edges.db`. These are **separate from** the existing wiki-link graph (NetworkX DiGraph), which continues to handle `[[wiki-link]]` relationships, link resolution, and the `lithos_links` tool.

LCMA edge fields:

- `from_id`, `to_id`
- `type`: `related_to`, `supports`, `contradicts`, `is_example_of`, `depends_on`, `analogy_to`, etc.
- `weight` (float)
- `provenance` (who created it: user/agent/rule — mirrors the `author`/`contributors` pattern on notes)
- `evidence` (anchors/snippets)

### Concept Node (emergent)

A regular `KnowledgeDocument` with `note_type: "concept"` — created via `lithos_write` (extended with an optional `note_type` parameter). Summarizes a cluster and links canonical examples via `is_example_of` edges in `edges.db`.

## 1.2 Retrieval: PTS-lite

PTS is exposed as a **new** `lithos_retrieve` tool. The existing `lithos_search` (Tantivy full-text) and `lithos_semantic` (ChromaDB) tools remain available for direct use by clients.

**Scouts** (parallel candidate generators, mapped to existing infrastructure):

- vector similarity top-k → `ChromaIndex.search()` (existing)
- lexical match → `TantivyIndex.search()` (existing)
- tags/metadata → `KnowledgeManager.list_documents()` with filters (existing)
- graph neighbors → `KnowledgeGraph.get_links()` (NetworkX, existing) + `edges.db` queries (new)
- recency → sort by `updated_at` from knowledge manager (existing)
- analogy → new (frame extraction, no existing equivalent)
- exploration → new (novelty/random sampling)

**Terraces**:

- Terrace 0: union candidates (cheap)
- Terrace 1: fast re-rank (diversity + priors)
- Terrace 2: optional LLM interpretive pass for final selection

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

Key rule: apply penalties **contextually first** (per query type / namespace) before global decay.

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
- per-agent WM + per-task shared WM (linked to existing `task_id` from coordination)
- write policies for reinforcement into shared memory (can gate via existing claim mechanics)

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
	- NetworkX wiki-link graph in `data/.graph/` (existing, pickle-cached)
	- LCMA typed edges in `data/.lithos/edges.db` (new)

3. **Retrieval layer (PTS — new, alongside existing tools)**
	- New `lithos_retrieve` tool orchestrates scouts
	- Existing `lithos_search` and `lithos_semantic` remain as direct-access tools
	- Terraces re-rank and optionally interpret

4. **Learning layer (new)**
	- reinforcement/penalties (via `stats.db`)
	- consolidation and decay
	- concept formation

5. **Governance layer (extends existing coordination)**
	- namespaces and access control (new filtering on existing tools)
	- contradiction workflow (new, extends existing `supersedes` field)
	- provenance and audit receipts (`receipts.jsonl`)

---

# 4) Minimal On-Disk Representation

This is intentionally “minimum viable” while supporting v1 + v2.

## 4.1 Directory Layout

The existing Lithos directory structure is preserved. LCMA consolidates all SQLite databases under `data/.lithos/` (coordination.db is moved from `data/` root; see migration note in §6 MVP 1).

```
data/                              # configurable via StorageConfig.data_dir
  knowledge/                       # existing: Markdown notes (subdirs allowed via `path` param)
    shared/                        #   optional namespace convention
    project/<project_name>/        #   optional namespace convention
    agent/<agent_id>/              #   optional namespace convention
  .tantivy/                        # existing: Tantivy full-text index
  .chroma/                         # existing: ChromaDB embeddings (all-MiniLM-L6-v2)
  .graph/                          # existing: NetworkX wiki-link graph (pickle)
  .lithos/                         # SQLite stores + LCMA data
    coordination.db                #   agents, tasks, claims, findings (moved from data/ root)
    edges.db                       #   typed weighted edges (separate from NetworkX)
    stats.db                       #   usage stats, salience, decay
    receipts.jsonl                 #   Auditor logs
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
access_scope: "project"                          # default: "shared"
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

Stored in `data/.lithos/edges.db`. This is **separate from** the existing NetworkX wiki-link graph (`data/.graph/graph.pickle`), which continues to power `lithos_links` and wiki-link resolution.

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

New MCP tools for LCMA edges (do not modify existing `lithos_links`):

- `lithos_edge_create` — create/update a typed edge
- `lithos_edge_list` — query edges by node, type, or namespace
- `lithos_edge_update` — adjust weight or conflict state
- `lithos_conflict_resolve` — resolve a contradiction edge

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
- `per_queryclass_priors` TEXT (JSON map)


Table: `coactivation`

- `node_id_a` TEXT
- `node_id_b` TEXT
- `namespace` TEXT
- `count` INTEGER
- `last_at` TEXT  
    PK `(node_id_a, node_id_b, namespace)`

## 4.5 Embeddings Store

Lithos already uses **ChromaDB** (`data/.chroma/`) with `all-MiniLM-L6-v2` (sentence-transformers) and cosine similarity. Documents are chunked (~500 char target, ~1000 max) and stored with metadata (`doc_id`, `chunk_index`, `title`, `path`, `author`, `tags`).

LCMA extends this with embedding space versioning via **separate ChromaDB collections** per space:

- Current collection: `knowledge` (existing, preserved)
- Versioned collections: `knowledge_<embedding_space_id>` (e.g., `knowledge_emb_v2_2026`)
- Active spaces tracked in config (not per-note frontmatter — avoids churn on re-embedding)
- During migration, the vector scout queries multiple collections and merges results

This avoids introducing a separate SQLite or FAISS store — ChromaDB already handles ANN search, persistence, and cosine similarity.

## 4.6 Retrieval Receipts (Auditor)

Append-only JSONL: `data/.lithos/receipts.jsonl`

```json
{
  "ts":"2026-02-26T18:12:01Z",
  "query":"add dynamic memory management to lithos",
  "namespace_filter":["project/lithos","shared"],
  "query_class":"design",
  "temperature":0.42,
  "scouts_fired":["vector","lexical","graph","analogy","exploration"],
  "candidates_considered":97,
  "terrace_reached":2,
  "final_nodes":[
    {"id":"node_7f3a9c","reason":"concept gateway + lexical match + high salience (damped)"},
    {"id":"node_a12b77","reason":"analogy scout: similar tradeoff pattern in different subsystem"}
  ],
  "conflicts_surfaced":[{"edge_id":"edge_19cc","state":"unreviewed"}]
}
```

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
    query_class: str            # e.g., "debug", "design", "planning", "write"
    max_context_nodes: int

class Candidate:
    node_id: str
    score: float
    reasons: list[str]
    scouts: list[str]

class RetrievalResult:
    final_nodes: list[Candidate]
    temperature: float
    terrace_reached: int
    receipt: dict
```

---

## 5.2 Scout Interface

Each scout wraps existing Lithos infrastructure where possible. Implementation notes in comments.

```python
def scout_vector(q: QueryContext, k: int) -> list[Candidate]:
    # Wraps existing ChromaIndex.search(query, limit=k, threshold, tags)
    # Queries active embedding spaces (multiple ChromaDB collections during migration)
    ...

def scout_lexical(q: QueryContext, k: int) -> list[Candidate]:
    # Wraps existing TantivyIndex.search(query, limit=k, tags, author, path_prefix)
    ...

def scout_tags_meta(q: QueryContext, k: int) -> list[Candidate]:
    # Wraps existing KnowledgeManager.list_documents(tags, author, path_prefix)
    ...

def scout_graph(q: QueryContext, seed_nodes: list[str], k: int) -> list[Candidate]:
    # Queries BOTH:
    #   - existing KnowledgeGraph.get_links(id, direction, depth) for wiki-link neighbors
    #   - new edges.db for typed/weighted edge neighbors
    ...

def scout_recency(q: QueryContext, k: int) -> list[Candidate]:
    # Wraps existing KnowledgeManager.list_documents(since=...) sorted by updated_at
    ...

def scout_analogy(q: QueryContext, k: int) -> list[Candidate]:
    # NEW: no existing equivalent — frame extraction + structural matching
    ...

def scout_exploration(q: QueryContext, k: int, mode: str) -> list[Candidate]:
    # NEW: mode in {"novelty","random","mixed"}
    ...

def scout_contradictions(q: QueryContext, seed_nodes: list[str]) -> list[str]:
    # Queries edges.db for type="contradicts" edges
    ...
```

Notes:

- All scouts must apply `namespace_filter` and `access_scope` gating **before returning** candidates.
- Existing search tools (`lithos_search`, `lithos_semantic`) remain available for direct client use.
    

---

## 5.3 Temperature (Coherence)

Reviewer suggestion, implemented as coherence among top candidates (by edges).

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

## 5.4 Retrieval: PTS Terraces

```python
def retrieve_pts(q: QueryContext) -> RetrievalResult:
    receipt = init_receipt(q)

    # -------- Terrace 0: parallel scouts (cheap) --------
    # Base k's can be tuned by query_class and temperature later.
    cands = []
    cands += scout_vector(q, k=12)
    cands += scout_lexical(q, k=12)
    cands += scout_tags_meta(q, k=8)
    cands += scout_recency(q, k=6)
    cands += scout_analogy(q, k=8)

    # Exploration scout (unified interface, but log mode)
    # Start with novelty bias before temp is known.
    cands += scout_exploration(q, k=6, mode="novelty")

    pool = merge_and_normalize(cands)             # merges by node_id, normalizes per scout
    receipt["candidates_considered"] = len(pool)
    receipt["scouts_fired"] = scouts_used(pool)

    # Seed nodes for graph expansion: top N from pool
    seed = top_ids(pool, n=10)
    pool += merge_and_normalize(scout_graph(q, seed, k=18))

    # -------- Terrace 1: fast re-rank (no LLM) --------
    ranked = rerank_fast(q, pool)                 # diversity, priors, concept damping, type priors
    top = ranked[:30]

    # Temperature after we have a coherent set to measure
    coherence = compute_coherence([c.node_id for c in top[:12]], namespace=dominant_namespace(q))
    temp = compute_temperature(coherence)
    receipt["temperature"] = temp

    # Adjust exploration based on temperature (v2)
    if temp > 0.6:
        pool += merge_and_normalize(scout_exploration(q, k=8, mode="mixed"))
        ranked = rerank_fast(q, pool)
        top = ranked[:30]

    # Contradictions check
    conflict_edges = scout_contradictions(q, seed_nodes=[c.node_id for c in top[:10]])
    receipt["conflicts_surfaced"] = conflict_edges

    # Decide whether to go to Terrace 2 (LLM)
    terrace = 1
    if should_use_llm_pass(q, temp, conflict_edges):
        terrace = 2
        final = llm_interpretive_select(q, top)   # choose 8-15, identify bridges, request follow-ups
        # optional targeted follow-up retrieval based on LLM prompts if still high temp
        if final.confidence < 0.5 and temp > 0.6:
            extra = targeted_followups(q, final.followup_queries)
            ranked2 = rerank_fast(q, top + extra)
            final = llm_interpretive_select(q, ranked2[:40])
        final_nodes = final.nodes[:q.max_context_nodes]
    else:
        final_nodes = top[:q.max_context_nodes]

    receipt["terrace_reached"] = terrace
    receipt["final_nodes"] = summarize_reasons(final_nodes)

    write_receipt(receipt)

    return RetrievalResult(
        final_nodes=final_nodes,
        temperature=temp,
        terrace_reached=terrace,
        receipt=receipt
    )
```

### `should_use_llm_pass` policy (v2)

```python
def should_use_llm_pass(q, temp, conflict_edges) -> bool:
    if conflict_edges:
        return True
    if q.query_class in {"design", "synthesis", "decision"} and temp > 0.25:
        return True
    if temp > 0.6:
        return True
    return False
```

This reflects my disagreement: **low temp does not automatically trigger LLM**.

---

## 5.5 Fast Re-rank (Terrace 1)

```python
def rerank_fast(q: QueryContext, pool: list[Candidate]) -> list[Candidate]:
    out = []
    for c in pool:
        stats = get_node_stats(c.node_id)
        meta  = read_frontmatter(c.node_id)

        type_prior = note_type_prior(meta.note_type, q.query_class)
        scope_prior = namespace_affinity(meta.namespace, q.namespace_filter)
        concept_damp = concept_penalty_if_overused(meta.note_type, c.node_id, q)

        decay_boost = spaced_repetition_boost(stats)
        ignore_pen  = ignored_penalty(stats, q.query_class)
        mislead_pen = misleading_penalty(stats, q.query_class)

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

---

## 5.6 Learning Updates After a Task (Bidirectional)

Called after an agent produces output.

Inputs:

- retrieval result (final_nodes + receipt)
- output text
- optional user feedback / acceptance
- agent’s explicit citations (best if you can enforce)

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
        # penalize per query class first
        adjust_queryclass_prior(nid, q.query_class, delta=-0.03)
        # mild salience decay if chronic
        if stats.ignored_count > 5 and stats.ignored_count > stats.retrieval_count:
            stats.salience = clamp(stats.salience - 0.02, 0.0, 1.0)
        write_node_stats(nid, stats)

def penalize_misleading(node_ids: set[str], q: QueryContext):
    for nid in node_ids:
        stats = get_node_stats(nid)
        stats.misleading_count += 1
        adjust_queryclass_prior(nid, q.query_class, delta=-0.08)
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

Consolidation hooks into the existing task lifecycle. A natural trigger is when `lithos_task_complete` is called — the coordination system already tracks `task_id` and `agent` for each task.

Run:

- at task boundaries (triggered by `lithos_task_complete`)
- on schedule
- or when WM exceeds size threshold

```python
def consolidate(task_id: str, agent_id: str):
    # WM = nodes retrieved/used during this task (tracked via receipts.jsonl)
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

Concept nodes are regular `KnowledgeDocument` notes with `note_type: "concept"`, created via `lithos_write`. Member links are `is_example_of` edges in `edges.db`.

```python
def maybe_update_concepts(namespace: str):
    # identify clusters based on coactivation graph (from stats.db)
    clusters = detect_stable_clusters(namespace, min_size=5, min_coactivation=3)
    for cluster in clusters:
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

def retrieval_should_surface_conflicts(query_class: str) -> bool:
    return query_class in {"design","decision","synthesis","debug"}

def surface_conflicts(nodes: list[str], q: QueryContext) -> list[str]:
    if not retrieval_should_surface_conflicts(q.query_class):
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

Each note has `schema_version` (default: 1 for existing notes without it). A migration registry in `data/.lithos/migrations/registry.json`:

```json
{
  "current_version": 2,
  "migrations": [
    {"from": 1, "to": 2, "name": "add_namespace_access_scope_note_type"}
  ]
}
```

Migration runner (uses existing `KnowledgeManager` for file I/O):

```python
def migrate_note(note_path: str):
    meta, body = read_frontmatter(note_path)    # existing python-frontmatter parsing
    v = meta.get("schema_version", 1)            # missing = version 1
    while v < CURRENT_SCHEMA_VERSION:
        meta, body = apply_migration(v, v+1, meta, body)
        v += 1
    write_note(note_path, meta, body)
```

Migrations must be idempotent and never remove existing fields.

---

# 6) MVP Roadmap (so this doesn’t explode)

Each MVP builds on the existing Lithos infrastructure. Existing tools are extended with backward-compatible optional parameters (e.g., `lithos_write` gains optional `note_type`, `namespace`, `access_scope` params with defaults that preserve current behavior). No existing parameters, return formats, or behaviors are changed.

## MVP 1 (3 scouts + Terrace 1 — wraps existing engines)

- Move `coordination.db` from `data/` root into `data/.lithos/` (auto-migrated on first startup)
- `lithos_retrieve` tool orchestrating scouts internally
- Scouts: vector (ChromaDB), lexical (Tantivy), tags/recency (KnowledgeManager)
- Basic rerank with `note_type` priors (requires new optional frontmatter fields)
- Extend `lithos_write` with optional LCMA frontmatter params (`note_type`, `namespace`, `access_scope`, etc.)
- `data/.lithos/receipts.jsonl` logging
- `data/.lithos/edges.db` (related_to) + basic reinforcement
- `data/.lithos/stats.db` (node_stats, coactivation)
- New tools: `lithos_edge_create`, `lithos_edge_list`, `lithos_node_stats`
- Schema versioning: `schema_version` field + migration registry

## MVP 2 (v2 essentials)

- Negative reinforcement on ignored/misleading (stats.db updates)
- Contradiction edges with `unreviewed` state + `lithos_conflict_resolve` tool
- Namespace + access_scope filtering on scouts
- Consolidation hook on `lithos_task_complete`
- Graph scout querying both NetworkX and edges.db

## MVP 3 (Hofstadter-flavored)

- Analogy scout (frame extraction — new, no existing equivalent)
- Temperature-based exploration depth
- Concept nodes (regular notes with `note_type: "concept"`) + damping
- Embedding space versioning via ChromaDB collections

---

# 7) Implementation Notes for Lithos Specifically

- Keep Markdown as source of truth for _content_ and stable metadata.
- Keep sqlite stores as truth for _dynamic signals_ (weights, stats, receipts).
- Make every agent write action policy-gated:
    - “agent can propose; librarian/auditor confirms” if you want strictness
    - Can leverage existing claim mechanics (`lithos_task_claim`) for write gating
- Treat **retrieval receipts** as a key product feature:
    - debugging, trust, and future auto-tuning all depend on them.

## Alignment with Existing Lithos (preserving backward compatibility)

### Existing tools preserved (20 tools, no renames or removals)

- Knowledge: `lithos_write`, `lithos_read`, `lithos_delete`, `lithos_search`, `lithos_semantic`, `lithos_list`
- Graph: `lithos_links`, `lithos_tags`
- Agents: `lithos_agent_register`, `lithos_agent_info`, `lithos_agent_list`
- Coordination: `lithos_task_create`, `lithos_task_claim`, `lithos_task_renew`, `lithos_task_release`, `lithos_task_complete`, `lithos_task_status`
- Findings: `lithos_finding_post`, `lithos_finding_list`
- System: `lithos_stats`

### New LCMA tools (additive only)

- `lithos_retrieve` — PTS-based retrieval orchestrating scouts
- `lithos_edge_create`, `lithos_edge_list`, `lithos_edge_update` — typed edge CRUD
- `lithos_conflict_resolve` — contradiction resolution
- `lithos_node_stats` — view/query salience and usage stats
- `lithos_receipts` — query retrieval audit history

### Existing frontmatter fields preserved

All existing `KnowledgeMetadata` fields (`id`, `title`, `author`, `created_at`, `updated_at`, `tags`, `aliases`, `confidence`, `contributors`, `source`, `supersedes`) are kept unchanged. LCMA adds optional fields with defaults.

### Existing infrastructure preserved

| Component | Location | Role in LCMA |
| --- | --- | --- |
| Tantivy | `data/.tantivy/` | Lexical scout backend |
| ChromaDB | `data/.chroma/` | Vector scout backend, extended with collection-per-space versioning |
| NetworkX | `data/.graph/` | Wiki-link graph scout (alongside new edges.db) |
| coordination.db | `data/.lithos/coordination.db` | Agent registry, task lifecycle, WM integration point |

### Key design decisions

- **`confidence` vs `salience`**: `confidence` (frontmatter) = author's belief about accuracy. `salience` (stats.db) = retrieval utility learned from usage. Both are 0–1 floats but serve different purposes.
- **NetworkX vs edges.db**: NetworkX handles structural `[[wiki-link]]` navigation and powers `lithos_links`. edges.db handles semantic/learned relationships with weights and types. Both are queried by the graph scout.
- **Frontmatter vs stats.db**: Static metadata in frontmatter (author, tags, note_type). Dynamic signals in stats.db (salience, retrieval_count, decay). This avoids constant file rewrites from learning updates.
- **Concept nodes**: Regular `KnowledgeDocument` notes with `note_type: “concept”`, not a separate entity type. Created via standard `lithos_write`.
