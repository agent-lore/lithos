# Typed-edge adjudicator: model decision runs, 2026-11-17

Runbook step 4 (`docs/runbooks/llm-model-migration.md`), task 70610498. All
four runs scored the same evidence on main 79d0aa0:

- **Cases:** the production golden set, 307 cases.
- **Labels:** labels v1, 181 reviewed slots, of which 29 are positive and 20
  have a scored direction.
- **Settings:** each model ran at its default settings, the incumbent's being
  production's, apart from the reasoning effort named in the table.

The three gate reports are in the `20261117-compare-*.md` files beside this
one.

| Run | Gate vs incumbent | Relation agreement | Precision | Recall | Direction | Failures | Edges/call | $ per 1k calls | Latency p50 / p95 |
|---|---|---|---|---|---|---|---|---|---|
| gpt-5-mini (incumbent, prod settings) | — | 42.0% | 15% (18/122) | 62% (18/29) | 10% (2/20) | partial 0.7% | 3.16 | 2.76 | 16.5 s / 143 s |
| gpt-6-luna, effort `none` | NOT PROMOTABLE: parse_error | 84.5% | 45% (10/22) | 34% (10/29) | 5% (1/20) | parse_error 1.0% | 0.47 | 0.21 | 2.8 s / 4.0 s |
| **gpt-6-luna, effort `low`** | **PROMOTABLE** | 85.1% | 41% (14/34) | 48% (14/29) | 20% (4/20) | none | 0.83 | 0.31 | 5.4 s / 8.1 s |
| gemini-2.5-flash-lite (sanity check) | NOT PROMOTABLE: error, parse_error, partial | 73.5% | 20% (10/50) | 34% (10/29) | 25% (5/20) | error 7.2%, parse_error 1.0%, partial 1.0% | 1.17 | 0.22 | 1.2 s / 1.5 s |

How to read the columns:

- **Relation agreement** is over the 181 labelled slots.
- **Precision** is the share of relations named on labelled slots that match
  the label.
- **Recall** is the share of the 29 positive labels found with the right
  relation.
- **Direction** is over the 20 scored directions; a miss on the relation is a
  miss on the direction too.

The four runs together cost about $1.10 of OpenRouter credit.

## Findings

1. **Luna at `low` passes every check.**
   - **Cost:** 89% cheaper per call than the incumbent.
   - **Speed:** p50 latency is a third of the incumbent's.
   - **Format:** no parse errors, partial answers or contract violations.

2. **Luna at `none` fails on parse errors only.** In 3 of 307 cases it wrote
   its reasoning into the reply as prose, which the parser rejects; one reply
   starts "We need classify. Need directions…". At `low` it has a reasoning
   channel, and the leak is gone.

3. **Relation agreement mostly measures `none`.** 152 of the 181 labels are
   `none`, so answering `none` everywhere would score 84.0%. Precision and
   recall on named relations are the clearer view:
   - Luna at `low` names a relation 34 times on labelled slots and is right 14
     times. The incumbent names 122 and is right 18 times.
   - Luna trades about a quarter of the incumbent's recall (48% vs 62%) for
     nearly three times its precision (41% vs 15%).

4. **The graph would grow more slowly.** Edges written per call at the 0.6
   floor fall from 3.16 to 0.83, or 74% fewer. Under the strict labels most of
   the dropped edges are ones the labels reject, but some true positives go
   with them.

5. **Direction is weak evidence on these labels.**
   - **The labels lean one way.** Of the 20 scored directions, 16 are
     `focus_to_candidate`: the conventions make the newer note A, and the
     focus is usually the newer note.
   - **The OpenAI models lean the other way.** They answer
     `candidate_to_focus` on about three quarters of their directed answers:
     gpt-5-mini 491 of 645, Luna at `low` 151 of 201.
   - **Gemini's known fault helps it here.** It answers `focus_to_candidate`
     almost always (331 of 351), which is its known failure, and that gives it
     the best direction score on these labels.
   - **So the sanity check failed the gate for the wrong reasons:** its 22
     errors (all provider 429s), plus 3 parse errors and 3 partial answers,
     not its direction. Against the reference, its direction score (6.1%)
     does show the failure.
   - **What to do about it:** the prompt never states the conventions.
     Follow-up a06c08c0 should add direction guidance and more directed labels
     that point `candidate_to_focus`.

6. **Stalls.** Calls that hit the 120 s ReadTimeout and succeeded on their
   one retry:

   | Run | Calls |
   |---|---|
   | gpt-5-mini | 28 (9%) |
   | Luna at `none` | 4 |
   | Luna at `low` | 8 |
   | gemini | 0 |

   The Luna stalls came in simultaneous bursts across the parallel runs. The
   incumbent's p95 of 143 s comes from its stalls.

7. **Confidence floor: keep 0.6.**
   - On the labelled slots, no model returned a named relation below 0.6.
     Across all slots the floor does drop a few answers: 9 for gpt-5-mini,
     5 for gemini and 1 for each Luna run. None of them is on a labelled
     slot, which is why the below-0.6 calibration bucket is empty.
   - Luna at `low` agrees 40% of the time in the 0.6–0.8 bucket and 42% at
     0.8 and above (n = 10 and 24), so its confidence does not separate right
     from wrong at this sample size.
   - A higher floor would cut recall without improving precision.

8. **Small sample.** There are 29 positive labels and 20 scored directions,
   so differences of one or two slots are noise. The precision gap is not.

## Recommendation

Run a staging canary of Luna with `reasoning_effort=low` (runbook step 5),
and watch for:

- `parse_error` at 0;
- edges per call near 0.8;
- p50 latency near 5 s.

Effort `none` is cheaper still ($0.21 per 1,000 calls), but it would need the
JSON contract enforced (structured outputs) or a parser that tolerates text
before the JSON, and both are prompt or parser changes for another day.
