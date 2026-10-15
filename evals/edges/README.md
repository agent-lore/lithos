# Typed-edge adjudication golden set

Artefact: the relation/direction/confidence judgements the enrich worker
asks the LLM for in `lithos.lcma.edge_inference` (prompt version 1).

## Files

| File | What |
|---|---|
| `cases.jsonl` | One case per line: the focus note, its numbered candidates (snippets exactly as sent), the verbatim messages when exported from the call log, and the incumbent's judgements as `reference`. |
| `labels.jsonl` | One reviewed human label per `(case_id, candidate_index)`: `relation` (or `none`), `direction` for directed relations only, labeller `confidence`, whether the incumbent's rationale was acceptable, labeller and date. `status: proposed` rows are a labelling aid and are ignored by the scorer. |
| `recorded/*.jsonl` | `{case_id, completion}` pairs: recorded model output used by `make eval-smoke` to gate parser and scorer changes without a network call. |

Symmetric relations (`contradicts`, `analogy_to`) carry no direction.
Candidates a label set does not cover are not scored.

## Current contents: synthetic seed set

The six `seed-*` cases are hand-written (no third-party text) so the harness
has something to run before the production export lands. They cover every
relation at least once plus two deliberate negatives (`negative:near_duplicate`,
`negative:unrelated`). Their labels are correct by construction and carry
`labeller: "seed (synthetic)"`.

`recorded/seed-incumbent.jsonl` answers every seed case correctly except one
slot; `recorded/seed-constant-direction.jsonl` reproduces the failure mode seen
on `gemini-2.5-flash-lite` in the WS1 rollout (perfect mechanics, every
direction `focus_to_candidate`); `recorded/seed-malformed.jsonl` has one
unparseable completion. The smoke test asserts the scorer sees each.

## Production set (to do, task 4044f201)

1. `lithos eval export --since 7d --out evals/edges/cases.jsonl` on the
   staging data directory after the call log has accumulated ≥ 250 usable
   calls (~5–6 days at ~47 calls/day). Cases may contain snippets of
   third-party text (Dave, 2026-09-29).
2. Stratify by relation, confidence bucket and namespace; keep 200–300; add
   30 deliberate negatives (near-duplicates, unrelated pairs, same-source
   pairs) and 20 non-English pairs, tagged `negative:*` / `lang:*`.
3. Label a stratified ~100-case subset: `scripts/eval_labels.py sheet` writes
   a Markdown review sheet with proposed labels; Dave edits the `label:`
   lines; `scripts/eval_labels.py import` writes `labels.jsonl`. Proposed
   labels are never accepted unread. 10% double-labelled; record agreement
   here.
