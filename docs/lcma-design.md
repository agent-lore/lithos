---
tags:
  - agents
  - lithos
---
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

# 1) LCMA v1 (Baseline)

## 1.1 Core Objects

### Memory Node

A Markdown note representing a unit of memory. Minimum fields:

- `id`, `path`
- `created_at`, `updated_at`
- `content` (markdown)
- `frontmatter` (tags, entities, project)
- `summary_short` / `summary_long` (optional)
- `salience` (float, default 0.5)
- `usage_stats` (retrieval_count, last_used, coactivation counts)
- embeddings (at least one content embedding)

### Edge

A typed relationship:

- `from_id`, `to_id`
- `type`: `related_to`, `supports`, `contradicts`, `is_example_of`, `depends_on`, `analogy_to`, etc.
- `weight` (float)
- `provenance` (who created it: user/agent/rule)
- `evidence` (anchors/snippets)

### Concept Node (emergent)

Special node that summarizes a cluster and links canonical examples.

## 1.2 Retrieval: PTS-lite

**Scouts** (parallel candidate generators):

- vector similarity top-k
- lexical match
- tags/metadata
- graph neighbors
- recency
- random sampling

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

- Retriever, Interpreter, Librarian, Cartographer, Auditor

---

# 2) LCMA v2 (Review-driven upgrades)

This section includes the review’s sensible changes plus explicit disagreements.

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

Add:

- namespaces and memory visibility
- per-agent WM + per-task shared WM
- write policies for reinforcement into shared memory

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

## 2.2 Where I disagree (explicit)

### 1) “Novelty scout and Random scout should be unified”

**Agree on interface**, but internally I keep two modes for telemetry/learning:

- **Novelty**: diverse-but-relevant (MMR-style)
- **Random**: true serendipity

They look similar superficially, but they behave differently and you’ll want to learn separate weights.

### 2) “Low temperature → trigger LLM validation”

I’d invert the default:

- **High temperature** (low coherence) → go deeper / use LLM to resolve ambiguity
- **Low temperature** → may skip LLM for simple queries

LLM pass is a cost; coherence is your “do I need it?” indicator.

### 3) “Newer wins” as a contradiction rule

Timestamp is a signal, not a rule, except for a small set of operational notes (`task_record`, `status`).

Default remains: **surface + provenance + workflow**.

---

# 3) System Architecture

## 3.1 Layers

1. **Storage layer (Lithos vault)**
	- Markdown notes + frontmatter
	- small stores for edges, embeddings, logs, stats

2. **Index layer**
	- lexical index (BM25/trigram)
	- embedding indexes (per embedding space)
	- graph store queries

3. **Retrieval layer (PTS)**
	- scouts generate candidates 
	- terraces re-rank and optionally interpret    

4. **Learning layer**
	- reinforcement/penalties
	- consolidation and decay
	- concept formation

5. **Governance layer**
	- namespaces and access control
	- contradiction workflow
	- provenance and audit receipts    

---

# 4) Minimal On-Disk Representation

This is intentionally “minimum viable” while supporting v1 + v2.

## 4.1 Directory Layout

```
lithos_vault/
  notes/
    shared/
    project/<project_name>/
    agent/<agent_id>/
    task/<task_id>/            # optional task-shared WM snapshots
  .lithos/
    edges.sqlite               #
    embeddings/
      <embedding_space_id>.sqlite   # or faiss/chroma files, per space
    lexical.sqlite             # optional; can be rebuilt
    stats.sqlite               # usage stats, salience, decay
    receipts.jsonl             # Auditor logs
    migrations/
      registry.json            # schema migrations
```

### Why sqlite?

- still local-first
- fast updates for weights/stats
- avoids rewriting large markdown files just to adjust edge weights

## 4.2 Note Format (Markdown + frontmatter)

Example: `notes/project/lithos/memory/LCMA.md`

```yaml
---
id: "node_7f3a9c"
schema_version: 2
namespace: "project/lithos"
access_scope: "project"   # enum: agent_private|task|project|shared|user_private
note_type: "concept"      # observation|agent_finding|summary|concept|task_record|hypothesis
title: "Lithos Cognitive Memory Architecture"
tags: ["lithos", "memory", "pts", "agents"]
entities: ["Lithos", "Parallel Terraced Scan", "Hofstadter"]
created_at: "2026-02-26T18:00:00Z"
updated_at: "2026-02-26T18:10:00Z"
confidence: 0.7           # provenance-weighted confidence, not truth
status: "active"          # active|archived|quarantined
embedding_spaces:
  - "emb_v1_2026-02"
summaries:
  short: "PTS-style retrieval and learning for Lithos."
  long: "..."
---
# Lithos Cognitive Memory Architecture
...
```

## 4.3 Edges Store Schema (sqlite)

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

Option A (simple): sqlite table `embeddings`

- `node_id`
- `embedding_space_id`
- `vector` (BLOB)
- `kind` (`content|summary|title|entities`)
- `updated_at`

Option B: external ANN index per space (FAISS/Chroma/etc) with:

- manifest mapping `node_id -> internal_id`
- stored alongside in `.lithos/embeddings/<space>/...`

## 4.6 Retrieval Receipts (Auditor)

Append-only JSONL: `.lithos/receipts.jsonl`

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

```python
def scout_vector(q: QueryContext, k: int) -> list[Candidate]: ...
def scout_lexical(q: QueryContext, k: int) -> list[Candidate]: ...
def scout_tags_meta(q: QueryContext, k: int) -> list[Candidate]: ...
def scout_graph(q: QueryContext, seed_nodes: list[str], k: int) -> list[Candidate]: ...
def scout_recency(q: QueryContext, k: int) -> list[Candidate]: ...
def scout_analogy(q: QueryContext, k: int) -> list[Candidate]: ...
def scout_exploration(q: QueryContext, k: int, mode: str) -> list[Candidate]:
    # mode in {"novelty","random","mixed"}
    ...
def scout_contradictions(q: QueryContext, seed_nodes: list[str]) -> list[str]:
    # returns contradiction edge ids or conflicting node ids
    ...
```

Notes:

- all scouts must apply `namespace_filter` and `access_scope` gating **before returning** candidates.
    

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

Run:

- at task boundaries
- on schedule
- or when WM exceeds size threshold

```python
def consolidate(task_id: str, agent_id: str):
    wm_nodes = get_working_memory(task_id, agent_id)         # nodes touched/used in session
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

```python
def maybe_update_concepts(namespace: str):
    # identify clusters based on coactivation graph
    clusters = detect_stable_clusters(namespace, min_size=5, min_coactivation=3)
    for cluster in clusters:
        concept_id = find_or_create_concept_node(cluster, namespace)
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

```python
def embed_node(node_id: str, embedding_space_id: str):
    text = read_note_text(node_id)
    vec = embedding_model(embedding_space_id).embed(text)
    store_embedding(node_id, embedding_space_id, kind="content", vec=vec)

def migrate_embeddings(new_space: str, strategy: str):
    # strategy: "batch" | "lazy"
    if strategy == "batch":
        for node_id in all_nodes():
            embed_node(node_id, new_space)
    elif strategy == "lazy":
        set_default_embedding_space(new_space)
        # embed on edit or on retrieval touch
```

Retrieval queries multiple spaces during transition:

```python
def scout_vector(q: QueryContext, k: int) -> list[Candidate]:
    spaces = active_embedding_spaces()  # e.g., ["emb_v2", "emb_v1"]
    results = []
    for space in spaces:
        results += ann_search(space, q.query_text, k=k//len(spaces))
    return normalize(results)
```

---

## 5.11 Schema Versioning for Notes

Each note has `schema_version`. A migration registry in `.lithos/migrations/registry.json`:

```json
{
  "current_version": 2,
  "migrations": [
    {"from": 1, "to": 2, "name": "add_namespace_access_scope_note_type"}
  ]
}
```

Migration runner:

```python
def migrate_note(note_path: str):
    meta, body = read_frontmatter(note_path)
    v = meta.get("schema_version", 1)
    while v < CURRENT_SCHEMA_VERSION:
        meta, body = apply_migration(v, v+1, meta, body)
        v += 1
    write_note(note_path, meta, body)
```

---

# 6) MVP Roadmap (so this doesn’t explode)

## MVP 1 (2–3 scouts + Terrace 1)

- lexical + tags + vector
- basic rerank with note_type priors
- receipts.jsonl logging
- edges store (related_to) + basic reinforcement

## MVP 2 (v2 essentials)

- negative reinforcement on ignored/misleading
- contradiction edges with `unreviewed` state
- namespaces + access filters
- consolidation hook

## MVP 3 (Hofstadter-flavored)

- analogy scout (frame extraction)
- temperature-based exploration depth
- concept nodes + damping

---

# 7) Implementation Notes for Lithos Specifically

- Keep Markdown as source of truth for _content_ and stable metadata.
- Keep sqlite stores as truth for _dynamic signals_ (weights, stats, receipts).
- Make every agent write action policy-gated:
    - “agent can propose; librarian/auditor confirms” if you want strictness
- Treat **retrieval receipts** as a key product feature:
    - debugging, trust, and future auto-tuning all depend on them.
