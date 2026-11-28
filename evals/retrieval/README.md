# Retrieval golden set: `lithos_search` against `lithos_retrieve`

Artefact: the ranked results a caller gets from `lithos_search` (hybrid and
semantic modes) and from `lithos_retrieve` for the same query, at the same
depth, against the same snapshot of a vault. Task `b8a3f918` asks which of
them should be the default for knowledge search, overall and for callers
that supply a note only when it is relevant. K7.1(b) of
`docs/plans/lcma-connected-knowledge.md` asks the same of the Epic B gate.

## Files

| File | What |
|---|---|
| `cases.jsonl` | The public slice: one query per line with its caller, slice and scope filters, each method's ranked ids and scores (retrieve also with its scouts and the vector scout's similarity), the per-call latency, a title/path/text head for every pooled note, and the snapshot it was pooled from. Only queries whose pooled notes are all in the `influx` namespace are committed here. |
| `labels.jsonl` | Human labels on the public slice, one per (case, note): `relevant`, `related` or `not`, labeller, date, status. Only `status: reviewed` rows are scored. |
| `seed/` | A hand-computed synthetic set that `tests/test_eval_retrieval_smoke.py` pins the scorer on. |

The personal slices (Dave's queries, garuda's planning queries, the robot
recall turns) live in the private repo `agent-lore/lithos-evals` under
`lithos/retrieval/`, checked out beside this repo at
`../lithos-evals`. They use the same schema and are scored together with
the public slice; nothing in them is duplicated here.

## Method

1. **Queries.** A `queries.jsonl` per slice: `{query_id, query, caller,
   slice, scope: {namespace_filter?, tags?, path_prefix?}, source?}`.
   Scope is what the caller actually passed, so a method is scored on the
   question it was asked. `scripts/eval_retrieval_queries.py from-receipts`
   writes one from a copy of `stats.db`: the distinct recent queries of the
   agents named, with their namespace filter and receipt id.
2. **Pool.** `lithos --data-dir <copy> eval retrieve-pool --queries ...
   --out cases.jsonl` runs hybrid (the caller's default threshold), semantic
   (threshold 0, so a floor can be studied afterwards) and retrieve at
   `--depth 10` on a **copy** of the production data directory. Methods are
   interleaved per query and each call is timed. Retrieval runs with
   `record=False` and the enrich worker is not started, so the copy keeps
   its receipts, node stats and coactivation exactly as copied. Never point
   it at a directory a server has open.
3. **Label.** `scripts/eval_retrieval_labels.py sheet` writes a review
   sheet over the pooled pairs: the query, the note's title, path and text
   head, and where each method ranked it. Claude proposes a label for every
   pair blind (`--proposed`); the reviewer blind-labels a random sample
   (`--blind --sample N`) for agreement, then reviews the proposals.
   `import` writes `labels.jsonl`. A re-pooled set needs labels only for
   its new pairs (`--labels existing.jsonl`).
4. **Score.** `lithos eval retrieve-score --cases evals/retrieval/cases.jsonl
   --cases ../lithos-evals/lithos/retrieval/cases.jsonl --labels ... --floor
   0.35` writes `evals/runs/<ts>-retrieval.json` and prints the report.

## What is measured

Per method, overall and per slice:

- **nDCG@10**, graded: `relevant` 2, `related` 1, `not` 0. A pair without
  a label counts as `not`; the share of such slots in each method's top 10
  is reported as `unlabelled`, so a thin pool shows rather than flatters.
  Queries with no relevant note in their pool are left out of the mean
  and counted as `n` minus `n rel`.
- **P@3, P@5, MRR** on `relevant` only.
- **Latency** median and p90 per method, from the same host and run.
- **Relevance floor.** For callers that supply a note only when it fits:
  for each score a caller could floor on (semantic similarity, retrieve's
  composite score, the vector scout's similarity inside retrieve), the
  share of queries with a relevant note whose top result clears the floor
  and is relevant (`hit rate`) against the share of queries with nothing
  relevant where something clears it anyway (`false supply`), swept over
  thresholds and read at the caller's floor. Hybrid returns an RRF score
  and so is not a floor candidate.

## Labelling standard

`relevant`: the note answers the query or directly bears on it; the caller
would be glad to have it. `related`: same project or topic, and the caller would see why it came
back and might skim it for the purpose behind the query, but it does not
answer the query: the previous month's review when a month's roadmap was
asked for, the project's design doc when its current status was asked for.
It earns partial gain in nDCG and counts as a miss for precision and MRR.
`not`: everything else, including the wrong week's plan when a week was
named and notes that merely share the query's words. A newer copy of a relevant note does not demote the older one: label
what the note says, not where it sits in the vault's history. Label the pair, not the method: a note is
relevant or not regardless of who ranked it where.

## Status

First pooled set, 2026-11-23: 115 queries (Dave 27, garuda 16,
codex-agent-foundations 20, ai-agents-briefing 6, hema-events-scout 6, robot
40) pooled at depth 10 on copies of the production vault (7779 notes) and of
staging (robot slice). Only one query (`codex-agent-foundations-019`) pooled
nothing but `influx` notes, so the public `cases.jsonl` holds that one case
and the other 114 are in the private repo: the vault is mixed enough that
almost every top-10 touches a personal namespace. The queries that
produced the public case are in `queries.jsonl` beside it.

Labelled and scored, 2026-11-28: all 2252 pairs are `reviewed`. Every pair
was first proposed blind by Claude passes (two grades); Dave then labelled
100 random pairs blind (84% agreement, kappa 0.42, the proposals too
strict) and adjudicated the 20 differing pairs (94/100 exact, 97/100 on
relevant-versus-rest), which set the labelling standard above and replaced
`superseded` with `related`; a second Claude pass then split `related` out
of the proposed `not` pairs (146 of 1929). Dave's 100 labels carry
labeller `dave`, the rest `claude-code-dns`. The decision run is
`evals/runs/20261128-retrieve-vs-search.md`: hybrid search is the default,
semantic search with a 0.35 floor for supply-only-if-relevant callers,
retrieve re-measured after K0.1.
