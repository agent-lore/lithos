# Is `lithos_retrieve` a better default than `lithos_search`? Decision run, 2026-11-28

Task b8a3f918. The working assumption since LCMA phase 2 has been that
`lithos_retrieve` (scouts + rerank) should be the default for knowledge
search. This is the first measurement. The run document is
`20261128T162112Z-retrieval.json`; `evals/retrieval/README.md` describes
the set and the labelling standard.

**Decision: switch the default to `lithos_search`.** Hybrid search is the
default for general knowledge queries. Semantic search with a 0.35 floor is
the default for callers that supply a note only when it fits (robot recall,
priming). `lithos_retrieve` stays available for association-style
exploration and is re-measured after K0.1 (c837b021) lands.

## What was scored

- **Queries:** 115, pooled at depth 10 on 2026-11-23 against fresh copies of
  prod (Lithos 0.6.0, 7779 notes) and, for the robot slice, staging (5783
  notes). Every method ran in-process on the copy, retrieve as the query's
  own caller with recording off, so the live stores were not touched and
  the agent-private gate behaved as in production.
- **Slices:** dave 27 (`lithos_search` calls from Claude Code transcripts),
  garuda 16 (planning agent), codex-agent-foundations 20, ai-agents-briefing
  6 and hema-events-scout 6 (receipt queries; the two briefing agents
  reword one question each, so their slices were trimmed), robot 40 (raw
  spoken turns from the romulus a5eval sessions, 24 answerable and 16
  filler).
- **Pool:** 2252 query–note pairs (about 20 per query); 115 cases. One case
  pooled only `influx` notes and is in this repo; the other 114 are in the
  private `lithos-evals` repo (`lithos/retrieval/`, commit `5778915`).
- **Labels:** 2252, all `reviewed`. Grades `relevant` 2 / `related` 1 /
  `not` 0: 229 relevant, 154 related, 1869 not. Provenance below.
- **Metrics:** nDCG@10 (graded), P@3, P@5, MRR (relevant only), latency
  p50/p90 on the same host, and a relevance-floor sweep read at 0.35.

## Headline

| method | nDCG@10 | P@3 | P@5 | MRR | p50 ms | p90 ms |
|---|---:|---:|---:|---:|---:|---:|
| **hybrid** (`lithos_search`) | **0.764** | **0.362** | **0.275** | **0.839** | 177 | 260 |
| semantic (`lithos_search`, `semantic_only`) | 0.602 | 0.310 | 0.226 | 0.714 | **7** | **24** |
| retrieve (`lithos_retrieve`) | 0.450 | 0.200 | 0.153 | 0.644 | 216 | 289 |

Means are over the 82 queries with at least one relevant note in the pool;
the 33 queries with none (16 robot filler turns, 17 general) are counted
only in the floor sweep.

Paired per query, hybrid beats retrieve on nDCG@10 on 86 of 96 queries,
loses on 8, ties on 2. The mean difference is +0.31 nDCG, bootstrap 95%
interval [0.27, 0.36]. Hybrid beats semantic 67–18 (+0.16, [0.12, 0.21]);
semantic beats retrieve 70–23 (+0.15, [0.10, 0.20]). Retrieve finds 53% of
the relevant notes in its top 10; semantic 64%; hybrid 79%. Of hybrid's
top relevant notes, retrieve has 48 in its top 3, 19 at ranks 4–10, and
misses 14 altogether.

## Per slice

| slice | n (rel) | hybrid | semantic | retrieve |
|---|---:|---:|---:|---:|
| dave | 27 (16) | **0.680** | 0.491 | 0.349 |
| garuda | 16 (15) | **0.716** | 0.456 | 0.435 |
| codex-agent-foundations | 20 (19) | **0.742** | 0.449 | 0.315 |
| ai-agents-briefing | 6 (6) | **0.800** | 0.626 | 0.508 |
| hema-events-scout | 6 (6) | **0.971** | 0.852 | 0.626 |
| robot | 40 (20) | 0.839 | **0.869** | 0.616 |

nDCG@10. Hybrid leads every general slice. On the robot slice semantic and
hybrid are level (semantic +0.03, interval [−0.05, 0.11]) and both beat
retrieve; robot memories are one-sentence facts, where lexical overlap adds
little over the embedding.

## Why retrieve loses

Retrieve's ranking is diluted by scouts that return notes unrelated to the
query. Share of each scout's returned notes that were labelled relevant,
over the whole pool:

| scout | notes returned | relevant |
|---|---:|---:|
| scout_vector | 223 | 44% |
| scout_lexical | 335 | 32% |
| scout_provenance | 30 | 7% |
| scout_graph | 206 | 4% |
| scout_tags_recency | 46 | 2% |
| scout_coactivation | 547 | **1%** |
| scout_source_url | 80 | 0% |

Coactivation is the single largest source of candidates and almost never
relevant: this is the popularity loop K0.1 (c837b021) is meant to break,
visible in the 2026-10-04 side-by-side that raised this task and now
measured on the full set. The two content scouts together carry retrieve's
hits; the rerank does not recover from the rest. The eight queries where
retrieve beats hybrid are association-style (`garuda-003` weekly
priorities, `dave-024` belief tracking, robot "Game of Thrones" turns),
which is the use retrieve should keep.

Both hybrid and retrieve return nothing on 5 robot filler turns ("Hmm",
"this"), where there is no lexical hit and the semantic threshold of 0.3
cuts everything; semantic at threshold 0 always fills, which is why its
floor matters.

## Relevance floor

For callers that supply a note only when it fits. Hit rate: share of
queries with a relevant note whose top result clears the floor and is
relevant. False supply: share of queries with nothing relevant where
something clears the floor anyway.

All 115 queries, floor 0.35:

| signal | hit rate | false supply |
|---|---:|---:|
| semantic similarity | 0.585 | 0.394 |
| retrieve composite score | 0.341 | 0.394 |
| retrieve's vector-scout similarity | 0.415 | 0.182 |

Robot slice only (24 answerable, 16 filler), floor 0.35:

| signal | hit | false supply |
|---|---:|---:|
| semantic similarity | 17/20 | **0/20** |
| retrieve composite score | 10/20 | 7/20 |
| retrieve's vector-scout similarity | 17/20 | 0/20 |

(20 answerable turns have their relevant note in the pool; 4 do not.) On
the robot slice semantic similarity at 0.35 supplies a relevant memory on
17 of 20 answerable turns and nothing on every filler turn. Retrieve's
composite score cannot be floored: at 0.35 it supplies something on 7 of 20
filler turns while hitting half as often. Retrieve's internal vector
similarity floors as well as semantic search does, at 30× the latency.

## Decision and what changes

1. **General knowledge queries: `lithos_search` (hybrid) is the default.**
   Best ranking on every general slice, 177 ms median.
2. **Supply-only-if-relevant callers: `lithos_search` with
   `semantic_only` and a 0.35 similarity floor.** Robot recall and priming.
   7 ms median, clean floor on the robot slice. Robot-companion should move
   its recall path to this (it currently calls retrieve); see task 179e87d9
   for the query-quality follow-ups that the robot slice surfaced.
3. **`lithos_retrieve` is not the default.** It stays for association-style
   exploration ("what is connected to this") and as the subject of K0.1.
   Re-run this eval, same set and labels, when K0.1 lands; the comparison
   is `lithos eval retrieve-score` over the same `--cases`/`--labels` and
   the fingerprints in the run documents.
4. **Docs and skills** (`lithos` agent skill, `docs/`, robot-companion
   AGENTS.md) should say: search first; retrieve when you want the
   neighbourhood rather than the answer. Those edits are follow-ups to
   this PR.

The Morrow pilot (b7db6ba6) was waiting on this answer and is unblocked.

## Label provenance and caveats

- **Proposals:** every pair was first labelled blind (no proposals, no
  ranks) by nine Claude passes with a shared brief, two grades
  (relevant/not), on 2026-11-23.
- **Calibration:** Dave labelled 100 random pairs blind on 2026-11-27.
  Agreement with the proposals was 84% raw, Cohen's kappa 0.42, one-sided:
  14 of 16 disagreements were Dave relevant / proposal not, and Dave wrote
  `related`, not then a grade, five times. He then adjudicated the 20
  differing pairs with both grades visible: 94/100 exact and 97/100 on the
  relevant-versus-rest line. The adjudicated bar is the labelling standard
  in `evals/retrieval/README.md`; `related` replaced `superseded` as the
  gain-1 grade. The 97% figure is post-adjudication; the blind figure is
  84%.
- **Related pass:** the proposals never used `related`, so eleven Claude
  passes re-read the 1929 proposed-`not` pairs under the new standard with
  Dave's 20 adjudications as worked examples, moving 146 to `related` and
  one to `relevant` (`garuda-016`, the 2026-08-31 daily log recording the
  September roadmap). Rates ran 2–21% by slice; planning and briefing
  slices with dense same-period pools ran high. This tier moves nDCG only;
  P@k and MRR do not see it.
- **Status:** Dave's 100 labels are `reviewed` under labeller `dave`; the
  remaining 2152 are `reviewed` under `claude-code-dns` on the strength of
  the calibration. A second blind sample of ~50 fresh pairs after K0.1 is
  the cheap way to check drift.
- **Not measured:** result quality below rank 10, scope-filtered queries
  (every pooled query ran unscoped except as the caller did), Lithos
  versions other than 0.6.0, and the effect of K0.1.

Cost: no LLM calls; the pool took about four minutes per method on the
copies, the label passes about two hours of Claude time and about two
hours of Dave's.
