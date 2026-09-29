# LCMA phase 4 — connected knowledge

Status: **direction endorsed by Dave 2026-09-29**, drafted the same day from the
state review (`docs/reviews/2026-09-state-review.md` §6) and the literature
brief (`docs/reviews/2026-09-agent-memory-literature.md`). This is the live
LCMA plan: `archive/lcma-checklist.md` maps each unfinished MVP-3 item onto
a K-workstream below, or marks it dropped or re-scoped (see §4). Filed in
the `lithos-core` tracker on 2026-09-29 as Epic A `1173ae32` (§5, October),
Epic B `a91ef652` (§5, Q4, blocked by Epic A's checkpoint `e07120d1`) and
the eval-suite epic `ee397e6c` (`llm-eval-suite.md`); each task carries its
measurement in the description. `lcma-design.md` §6 "MVP 4" points here.

Context that shaped the priority order: the influx corpus is unread because
its owner has been waiting on Lens for a knowledge browser, so K1 (the
read-shape Lens K2 needs) is the item that turns intake into something he can
navigate, and K0 is what makes the neighbourhoods it renders trustworthy.

Supersedes MVP-3 WS3–WS6 in `lcma-design.md`: their sequencing is replaced
by the K-workstreams below, and §4 names the parts that are dropped or
re-scoped (exploration scout, analogy scout, weight fitting); does not change
the on-disk contract, the write contract, or the privacy-first / optional-LLM
decisions of 2026-07.

## 1. Problem

Lithos has a good retrieval engine, a background enrichment worker that now
writes real typed edges, and a corpus of 5,582 notes. Measured against the
goal — *a connected knowledge base for Dave and his agents* — three things
are wrong:

1. **Nobody reads the knowledge.** 76% of the corpus is influx intake; 314 of
   3,007 article notes have ever been read or returned by a search. The notes
   that are read are Dave's own planning notes, through one agent.
2. **Retrieval has drifted into a popularity loop.** Coactivation → working
   memory → consolidation salience → usage score → rerank rewards the same
   ~10 hub notes in 40% of all result slots; 32% of slots are filled by nodes
   found only by the coactivation scout. For influx, `lithos_retrieve` is
   worse than hybrid search on most of its slots.
3. **The graph is invisible.** 5,907 LLM-inferred edges with rationales exist
   in `edges.db`; `lithos_related` returns them as raw id rows, no consumer
   renders them, and 112 of 113 `contradicts` edges have never been seen by
   anyone.

Underneath: decay is applied uniformly to facts and episodes; nothing in the
system *compiles* what is known about a topic into a page a human would
revisit; and there is no measurement of whether memory helps.

## 2. Principles

- **Notes are the truth; everything else is an index.** Compiled pages are
  notes too (Obsidian-visible, provenance-stamped, reversible). No triple store
  as primary storage.
- **Knowledge is superseded; memory decays.** Declarative content and typed
  edges get validity intervals and deterministic supersession. Episodic
  material (receipts, working memory, coactivation, findings) decays and
  reinforces. Procedures are revised only on evidence.
- **LLM work is background, budgeted, optional and provenance-stamped**
  (unchanged from WS1). Nothing LLM-generated overwrites a human note.
- **Owner-visible outputs are Markdown notes** in the vault, so they work with
  Obsidian, the planning-coach agent and Lens without new UI.
- **Every workstream ships with its measurement.** Receipts are the
  instrumentation; each K-item names the number it moves. The pre-change
  baseline is `docs/reviews/2026-09-retrieval-baseline.json` (numbers, the
  ten hub notes, and a 47-query held-out set from the human-facing callers)
  with its narrative in the knowledge base
  (`analysis/lcma-retrieval-baseline-2026-09.md`, id `b4754360`).
- **Keep everything.** Intake is never aged out or pruned for being unread
  (Dave, 2026-09-29); storage is assumed sufficient and is out of scope here.
  Lint and digest may *flag* never-read material; they do not archive it.

## 3. Workstreams

### K0 — Make retrieval honest (prerequisite for everything else)

1. **Break the popularity loop.** Normalise the coactivation scout by node
   degree / total activations (PMI-style) or cap its normalised score below
   `vector`/`lexical`; stop the consolidation salience boost after a node has
   been consolidated N times; make `usage_score` recency-weighted with a cap.
   Reward multi-scout agreement (sum, not average, of contributing weights).
   Measure: top-10 share of result slots 40% → <15%; coactivation-only share
   32% → <10%; both queries are in the review.
2. **#421 + WS2 slice 2.** Set-difference backfill over `edge_inference_log`
   in `full_sweep`, budget-capped per sweep; treat 0-valid-judgement runs as
   not done; key re-inference on a content hash, not `doc_version`; add
   `lithos infer-edges --backfill | --purge`. Measure: focus coverage 30% →
   90% of notes within the daily budget.
3. **Feedback plumbing.** The `lithos` skill and influx pass `receipt_id` +
   `cited_nodes` on `task_complete`; `lithos_retrieve` surfaces `contradicts`
   neighbours by default (`surface_conflicts` becomes opt-out). Measure:
   `cited` > 0 on more than 1% of nodes within a month; contradictions
   reviewed per week > 0.
4. **Temperature that is not a constant.** `coherence = 1 − dispersion` of the
   top-k embedding scores; put it in the envelope and use it to widen
   Phase-B seeding on vague queries (WS3-lite). Measure: temperature variance
   > 0 on receipts.

### K1 — Read-shape (WS7, unchanged)

`lithos_related` returns ranked `neighbours` with `{id, title, note_type,
namespace, relation, source, direction, weight, rationale}`, filters
(`relation_types`, `sources`, `min_weight`, `limit`), opt-in `semantic` and
`coactivation` includes; `lithos_edge_list` gains `source`/`min_weight` and
endpoint titles; a `lithos_receipts` read. No migration. Freeze the shape so
Lens K2 builds against it.

### K2 — Knowledge vs memory tiers

- **Decay policy by note kind.** A table in `LcmaConfig`: `summary`,
  `concept`, `procedure`, `reference` never decay (superseded only);
  `observation`, `agent_finding`, `task_record`, receipts and coactivation
  decay with the existing floor. Prune coactivation pairs below a count/age
  threshold (measured free in the literature at ~10%).
- **Validity and supersession on edges and claims.** `valid_from`,
  `valid_to`, `superseded_by` on `edges.db` rows (nullable, no migration of
  meaning); an optional `claims:` frontmatter list of `(subject, relation,
  object, since)` for notes that state facts. Supersession is deterministic
  and never inferred: an older claim is retired only when a *dated* claim on
  the same `(subject, relation)` arrives from an authoritative channel
  (owner-written, or the same source updating itself), or when
  `lithos_conflict_resolve` records `superseded`. An inferred `contradicts`
  edge opens a conflict (`conflict_state=unreviewed`) and changes nothing
  else — both claims stay valid until resolved, as `lcma-design.md` §5.9
  already requires. Retired claims stay retrievable via `as_of=` on
  `lithos_related`. This is the real answer to GitHub #129 and most of #49.
- Measure: stale-fact rate on a synthetic evolving-facts set (K7) 0 vs the
  15–40% plain RAG shows.

### K3 — Compiled pages (WS4 reframed)

- **Entity pages.** For every NER entity that N ≥ k notes mention (after an
  entity-quality filter: the extractor emits fragments such as "Les", "Deux",
  "un autre" on non-English intake, so the threshold alone is not enough), the enrich
  worker writes or rewrites `entities/<slug>.md` (`note_type: entity`): a
  narrative profile compiled from the mentioning notes and their edges, with
  dates, numbers and ids kept verbatim, wiki-links to every source, and a
  `compiled_from` provenance list.
  **Decided (Dave, 2026-09-29): machine-owned pages, adopted on edit.**
  Entity and concept pages are separate notes authored by `lithos-enrich`
  and regenerated whole through the normal write path; human notes are never
  rewritten. If a human edits a page, its version no longer matches the
  worker's last write, the worker stops regenerating it, and lint reports it
  as adopted. Human commentary lives in the owner's own notes linking to the
  page. To be recorded as ADR-0009 as Epic B's first child.
- **Concept pages.** HDBSCAN over embeddings (thresholds already measured in
  `lcma-design.md` WS4), validated by coactivation and entity overlap;
  stable clusters get `concepts/<slug>.md` with a summary and member links.
  Gateway retrieval: `lithos_retrieve` returns the page plus specifics.
- The wiki-link graph grows from 123 edges to thousands as a side effect,
  which is what makes Obsidian's own graph useful again.
- Measure: time-to-answer "what do I know about X" with vs without pages;
  pages opened per week by the owner.

### K4 — Lint and digest (owner surface without UI)

- **`lithos lint`** (CLI first, tool later) writes `meta/open-questions.md`:
  unresolved `contradicts` edges with both sides, orphans (degree 0), claims
  past `valid_to`, entities above the page threshold with no page, stale
  notes, and gaps phrased as questions an agent can pick up as tasks.
- **Daily digest note** under `user/planning/digests/<year>/` (the planning-note
  convention the planning-coach agent already reads): what agents changed,
  claims superseded, contradictions opened, and 3–5 resurfaced notes chosen
  by recall probability (salience reinterpreted as p(recall) with a
  Readwise-style half-life) that connect to active projects. The
  planning-coach agent already reads that folder every morning.
- Measure: digest items acted on per week; lint questions converted to tasks.

### K5 — Gated priming for agents (task-graph Phase 4 `lithos_task_prime`)

On `task_claim` or session start, compute a brief: relevant claims, open
contradictions, procedures, and the last outcomes on structurally similar
tasks (WS5's analogy frames, extracted from the 1,348 task outcomes). Inject
only above a relevance threshold; otherwise return a one-line index. Measure:
token cost per primed task and "acted-on-retrieved-fact" rate on K7's task set.

### K6 — Procedural memory

`note_type: procedure` with SKILL.md-compatible frontmatter (`name`,
`description`, `when_to_use`, `verification`); promotion to `verified` only
after a completed task cites it; usage counts from receipts; an export that
materialises verified procedures into `.claude/skills/`. Claude Code's four
memory kinds (`user`, `feedback`, `project`, `reference`) become recognised
`note_type` values so agent memory notes are typed.

### K7 — Evaluation harness

K7 measures whether memory helps a caller. Model, prompt and cost evaluation
of the LLM artefacts themselves is the separate `llm-eval-suite.md` PRD; K3
does not generate pages until its runner exists.

1. Two measurements, not one. (a) **Prospective re-run**: execute the 47
   held-out queries (`docs/reviews/2026-09-retrieval-baseline.json`) against
   a staging copy with the candidate configuration and recompute the two
   baseline shares plus per-query kept/dropped results; needs no labels and
   is the day-one K0 measurement. (b) **Offline replay**, only once receipts
   capture the full candidate pool with per-scout scores (today they hold
   final results and a count) and a labelled held-out set exists. The
   committed 47 queries are not enough for a per-caller gate: no query is
   Dave's, the largest caller groups hold 15, 13 and 10 queries, and 26 of
   47 requested fewer than 10 results. So (b) first **extends the set**:
   at least 20 of Dave's own questions (collected via the planning-coach
   skill and Lens), each of the top three agents by retrieve volume topped
   up to at least 20 queries from newer receipts, and every query re-run on
   the staging copy at `limit=10` so results exist to depth 10 regardless
   of the caller's original limit. Dave labels each query's top 10
   (relevant / not / superseded) and agents report `used_ids`. Then nDCG@10
   on labelled items and exploration-use rate can compare scouts and
   rerankers without re-running. A changed scout can surface candidates
   the old receipts never saw, so (b) is not a substitute for (a).
2. A synthetic-owner ground-truth set with validity intervals and volatility
   classes (Ground Truth First style), rendered into notes and findings;
   questions instantiated mechanically; run at simulated 3- and 9-week tenure.
3. A small task-outcome A/B on real Lithos-repo tasks: memory off / search
   only / full LCMA, reporting accuracy, tokens and latency together.
4. Standing baselines: full-context and plain grep over the vault.
5. Judge hygiene: adversarial wrong-but-topical answers; prompts in-repo.

## 4. Not doing

Re-scoped or dropped from MVP-3, stated explicitly (review comment on PR
#431): the **exploration scout** (WS3) is dropped — K0.4 widens Phase-B
seeding on vague queries instead, and MMR/Thompson-style exploration returns
only if K7's exploration-use rate shows the neighbourhood is too narrow; the
**analogy scout** (WS5) is re-scoped from a retrieval scout to K5's priming
input (frames extracted from task outcomes feed the brief, not the result
list); **fitting `rerank_weights` from implicit signals** (WS6) is deferred
until K7.1(b) has labels — K0.1 is a hand-tuned repair, not metamemory.

GraphRAG community summaries; entity triples as primary storage; RL-trained
memory managers; decay on declarative notes; embedding-space versioning
(still dropped); LLM calls on the retrieve hot path; auto-overwriting human
notes; vendor benchmark scores as targets.

## 5. Sequence and epics

Two epics (agreed 2026-09-29), gated by structure rather than prose:

1. **Epic A — retrieval honest and visible (October-sized):** K0.1–K0.4, K1,
   read attribution on `lithos_read`/`search`/`list` (K4 and K7 need to know
   who acted), the conformance suite routed through a validating MCP client
   (so envelope changes in K1 are actually tested), the K7.1(a)
   prospective concentration check (re-run the 47 held-out queries on a
   staging copy; no relevance labels needed), and a dated **checkpoint
   task** that records the top-10 share and coactivation-only share after
   K0 ships and decides whether Epic B proceeds. The labelled quality
   comparison (K7.1(b)) is Epic B work, not an Epic A deliverable.
   **Built by Claude Code sessions** (Dave, 2026-09-29), not dispatched
   through loom, so tasks carry no `trigger:` tags and need no lithos-core
   sandbox image.
2. **Epic B — connected knowledge (Q4):** K2, K3, K4, K5, K6, K7.1(b), K7.2–5, blocked
   by the checkpoint. Its first children are ADR-0009 (machine-owned compiled
   pages, decided above) and the influx note-typing task, filed in the influx
   project (78% of the corpus is `note_type: summary`, so K2's decay-by-kind
   cannot differentiate until intake classifies; confirmed 2026-09-29). Digest-lite (K4's daily note without LLM
   summarisation) may be pulled forward into October if attention allows.

Within Epic A: `K0` → `K1`; K7.1(a) lands with K0.1. Each task carries its
measurement in the description.

## 6. Success criteria

- Epic A (measurable with the prospective check): on the 47-query held-out
  set (320 result slots) the top-10 share is under 15% and the
  coactivation-only share under 10%. Pinned baseline for that set: 34.7% /
  36.2% (`held_out_baseline` in the baseline file). The 30-day non-influx
  receipt population is a secondary, non-gating reading: 41.5% / 40.1%.
- Epic B (needs the K7.1(b) extended set, candidate receipts and labels):
  `lithos_retrieve` beats `lithos_search` on nDCG@10 over the labelled set,
  measured per caller for Dave and for each of the top three agents by
  retrieve volume, each with at least 20 labelled queries and results to
  depth 10. Pass = higher mean nDCG@10 for every one of those four callers.
- Every note has been a focus of edge inference at least once; contradictions
  are visible by default and reviewed weekly.
- `lithos_related` renders a typed, provenanced, titled neighbourhood that
  Lens K2 ships against.
- The owner opens compiled pages and acts on digest items week over week —
  the first evidence that Lithos is a knowledge base he revisits rather than
  a store his agents write into.
