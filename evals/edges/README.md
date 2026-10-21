# Typed-edge adjudication golden set

Artefact: the relation/direction/confidence judgements the enrich worker
asks the LLM for in `lithos.lcma.edge_inference` (prompt version 1).
Procedure: `docs/runbooks/llm-model-migration.md` steps 2–3.

## Files

| File | What |
|---|---|
| `cases.jsonl` | The production set: one case per line with the focus note, its numbered candidates (snippets exactly as sent), the verbatim messages when exported from the call log, and the incumbent's judgements as `reference`. |
| `labels.jsonl` | Human labels, one per `(case_id, candidate_index)`: `relation` (or `none`), `direction` for directed relations only, labeller `confidence`, whether the incumbent's rationale was acceptable, labeller and date. Only `status: reviewed` rows are scored. Not written yet (runbook step 3). |
| `seed/` | The synthetic seed set (`cases.jsonl`, `labels.jsonl`, `recorded/`) that `make eval-smoke` gates the parser and scorer on. |

Symmetric relations (`contradicts`, `analogy_to`) carry no direction.
Candidates a label set does not cover are not scored against labels; every
exported case is still scored against its `reference`.

## Production set v1 (shaped 2026-10-21)

**Source.** `lithos eval export --since 14d --model openai/gpt-5-mini`, run
inside the prod and staging containers. The window is 2026-10-16 02:05 to
2026-10-21 09:41 UTC, the first five days of the call log. Every exported call
was `ok` (270) or `partial` (1). The incumbent was `openai/gpt-5-mini` with
OpenRouter defaults (no `reasoning_effort`).

**Filters.**

| Step | Cases |
|---|---|
| Exported (prod 193, staging 112) | 305 |
| Dropped: the focus note or a candidate is outside the `influx` namespace (personal and agent notes; this repo is public) | −23 |
| Dropped: an older call for the same focus note (latest content wins) | −11 |
| **Exported cases kept** (prod 160, staging 111) | **271** |
| Hand-built negatives | +30 |
| Hand-built French-titled cases | +6 |
| **Total** | **307 cases, 1,402 slots** |

No further stratified down-sampling: 271 is inside the 200–300 target, and the
labelled subset (step 3) is where stratification matters.

**Incumbent's judgements on the 1,341 exported slots.** None of these labels
are human-reviewed yet.

| Relation | Slots | Share |
|---|---|---|
| supports | 371 | 28% |
| none | 352 | 26% |
| analogy_to | 325 | 24% |
| refines | 140 | 10% |
| is_example_of | 98 | 7% |
| depends_on | 43 | 3% |
| contradicts | 12 | 1% |

Confidence of the 989 non-`none` judgements: ≥0.8: 590, 0.6–0.8: 384,
<0.6: 15.

**Known skews.**

- **`analogy_to` at 24% looks inflated.** The prompt defines it as
  "structurally parallel solutions in different domains". Many of these are
  same-domain pairs, for example successive weekly "Aminet uploads until …"
  lists. The labels will show how much of this is wrong.
- **Contradictions are rare.** `contradicts` has only 12 slots, below the
  15-per-relation target; the traffic doesn't allow more.
- **Few all-`none` cases.** 10 of the 271 exported cases have no relation on
  any candidate. The hand-built negatives raise the `none` share.
- **Mostly one domain.** Candidates are dominated by retro-computing feeds:
  `amiga-news-de-english` is about a fifth of all candidate notes.
- **Same article in both environments.** 88 articles were ingested into both
  prod and staging. The two copies have different summaries and different
  candidate sets (they share 0–3 of 5 candidates), so both cases are kept.
  When picking the labelled subset, take at most one case per article.

**Hand-built cases.** These have `messages: null`, `prompt_version: null`
and `reference: null`. The runner rebuilds the prompt from the structured
fields. The rebuild was verified byte-identical against the verbatim prompts
of all 271 exported cases. Snippets are text the engine actually sent, taken
from the exported cases; `similarity` is omitted where unknown, since it is not
shown to the model. Each case's `source.note` says why it was built.

| Tag | Cases | Slots | Construction | Expected label |
|---|---|---|---|---|
| `negative:near_duplicate` | 5 | 5 | The same item as a different note: LessWrong/Alignment Forum crossposts, the same release reported by two outlets, a forum thread and its `Re:` reply | `none` (seed convention, to confirm in review) |
| `negative:same_source` | 12 | 18 | Same feed, different content: five recurring upload/installer lists for different weeks, and seven same-feed posts on different topics | `none` |
| `negative:unrelated` | 13 | 26 | Focus and candidates from different domains (retro computing, AI alignment, robotics, C64, SGI) | `none` |
| `lang:fr` (`fr-*`) | 6 | 12 | French-titled `amiga-impact` notes, with their top two `influx` neighbours from a prod semantic search run the way the engine queries | labelled in review |

**Non-English.** The corpus has no non-English text at the prompt level.
The 12 French notes are `amiga-impact` items whose influx summary is in
English, so only the title is French. Besides the six `fr-*` cases, eight
exported cases include one of those notes as a candidate and carry
`lang:fr`, giving 14 `lang:fr` cases and about 20 French-titled pairs.

## Labelling (to do, task 4044f201)

The subset is 24 exported cases (120 slots), picked greedily to cover every
incumbent relation, both environments and 16 feeds, with at most one case per
article. Every hand-built slot is labelled too, 61 more, so 181 in all.
Claude's blind proposals (made without seeing the reference) are on the local
review sheet. Neither the sheet nor the proposals are committed; only
`labels.jsonl` is. `scripts/eval_labels.py
sheet` writes the review sheet with proposed labels. Dave edits the `label:`
lines, and `scripts/eval_labels.py import` writes `labels.jsonl`. Proposed
labels are never accepted unread. About 10% of slots are double-labelled, and
the agreement is recorded here.

Conventions to settle in that review:

- **Near-duplicates** (the same item as another note): `none`. A duplicate
  is not evidence for itself. This is the seed set's convention.
- **Same recurring series, different content** (weekly upload lists): `none`.
- **Successive releases of the same software** (for example Directory Opus
  5.103 vs 5.101): `refines` (newer `focus_to_candidate`) or `none`. Not yet
  decided.
