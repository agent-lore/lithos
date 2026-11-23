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
| `labels.jsonl` | Human labels on the public slice, one per (case, note): `relevant`, `superseded` or `not`, labeller, date, status. Only `status: reviewed` rows are scored. |
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

- **nDCG@10**, graded: `relevant` 2, `superseded` 1, `not` 0. A pair without
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
would be glad to have it. `superseded`: it did, but a newer note replaces
it. `not`: everything else, including a shared topic, source or author
without bearing on the query. Label the pair, not the method: a note is
relevant or not regardless of who ranked it where.

## Status

Harness only (PR 1). The public `cases.jsonl` and `labels.jsonl` land with
the first pooled set.
