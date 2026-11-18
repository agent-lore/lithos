# Typed-edge adjudicator: prompt v2 decision runs, 2026-11-18

Task a06c08c0. This compares prompt v1 with prompt v2 on gpt-6-luna at
`reasoning_effort=low`, the model chosen on 2026-11-17
(`20261117-edges-model-decision.md`). A gemini run on v2 checks that the gate
still catches a weak model.

All three runs scored the same evidence:

- **Cases:** golden set v2, 328 cases. These are the 307 cases of v1, each
  with its notes' v2 view, plus 21 mirrored cases.
- **Labels:** labels v2, 202 reviewed slots: 51 positive, with 44 scored
  directions (23 `focus_to_candidate`, 21 `candidate_to_focus`).
- **Code:** v1 ran from main at f9b884d, whose prompt is v1, and v2 from the
  prompt-v2 branch. Their case and label fingerprints match.

`evals/edges/README.md` describes the golden set and labels v2. The gate
reports are `20261118-compare-luna-low-prompt-v1-vs-v2.md` and
`20261118-compare-luna-low-v2-vs-gemini-2.5-flash-lite-v2.md`.

| Run | Gate | Relation agreement | Precision | Recall | Direction | Failures | Edges/call | $ per 1k calls | Latency p50 / p95 |
|---|---|---|---|---|---|---|---|---|---|
| Luna `low`, prompt v1 (incumbent) | — | 78.7% | 44% (26/59) | 51% (26/51) | 25.0% (11/44) | none | 0.83 | 0.30 | 5.0 s / 7.8 s |
| **Luna `low`, prompt v2** | **PROMOTABLE** (quality path) | 87.6% | 84% (31/37) | 61% (31/51) | 54.5% (24/44) | none | 0.61 | 0.34 | 4.6 s / 8.3 s |
| gemini-2.5-flash-lite, prompt v2 (sanity check) | NOT PROMOTABLE vs Luna v2: relation, direction, partial, parse_error, cost | 79.2% | 50% (35/70) | 69% (35/51) | 31.8% (14/44) | partial 0.6%, parse_error 0.3% | 1.20 | 0.28 | 1.1 s / 1.5 s |

How to read the columns:

- **Precision** is the share of relations named on labelled slots that match
  the label.
- **Recall** is the share of the 51 positive labels found with the right
  relation.
- **Direction** is over the 44 scored directions; a miss on the relation is a
  miss on the direction too.

The three runs together cost about $0.30 of OpenRouter credit.

## Findings

1. **Prompt v2 is much more precise.**
   - **Precision** rises from 44% to 84%.
   - **False positives fall:** on the 151 slots labelled `none`, v2 names a
     relation 5 times, against 18 for v1.
   - **Recall** rises from 26 to 31 of the 51 positives.
   - **On the original 181 slots alone:** precision 40% → 75%, recall 17 →
     18 of 30.

2. **Direction is now mostly right.**
   - **When the relation is right, v2 gets the direction right** on 24 of 26
     scored directions, against 11 of 21 for v1.
   - **The lean has reversed:** v1 answered `candidate_to_focus` on 169 of its
     225 directed answers, while v2 answers `focus_to_candidate` on 132 of 185.
     That is what the conventions imply, because the focus is usually the
     newer note and the prompt now shows the dates.
   - **Mirrored cases:** v2 finds 13 of the 21 relations with no false
     positives and gets 11 of their 13 directions right, so it reads the dates
     rather than assuming an order.

3. **Confidence now means something.**
   - **v2:** a named relation at 0.6–0.8 matches the label 88% of the time
     (n = 8), and one at 0.8 or above 83% (n = 29).
   - **v1:** the figures were 38% (n = 21) and 47% (n = 38).
   - **The floor:** v2 named no relation below 0.6 on a labelled slot, so the
     floor stays 0.6.

4. **It costs a little more.**
   - **Tokens per call:** prompt tokens rise from 986 to 1,655, while
     completion tokens fall from 408 to 357, with less reasoning.
   - **Cost:** $0.34 against $0.30 per 1,000 calls, 14% more. At current
     traffic that is under a cent a day, and still 88% below gpt-5-mini's
     $2.76.
   - **Latency:** p50 latency fell slightly, from 5.0 s to 4.6 s.

5. **The gate needed a quality path.**
   - **Old rule:** `evals/POLICY.md` required a candidate to cost at least
     30% less, or no more with higher agreement. That suits a model swap but
     rejected v2.
   - **New rule (Dave, 2026-11-18):** a candidate may cost up to 25% more
     when relation agreement rises by at least 3 points and direction
     agreement by at least 5 points, with no failure rate rising.
   - **Result:** v2 gains 8.9 and 29.5 points and passes.

6. **The sanity check now fails on quality.**
   - **v1 golden set:** gemini failed only on failure rates, because the
     labels' directions leaned the way it always answered.
   - **Golden set v2:** it fails on relation, direction, partial and
     parse-error rates, and cost. It names a relation on 26 `none` slots and
     gets 14 of 30 directions right where its relation is right.
   - **Prompt v2 changes gemini too:** it no longer answers one direction
     regardless (211 `candidate_to_focus` to 172 `focus_to_candidate`).

7. **Fewer edges.**
   - **Rate:** edges written per call fall from 0.83 to 0.61. Most of the
     drop is false positives that v2 no longer names.
   - **Sample size:** with 51 positive labels and 44 scored directions, a
     difference of one or two slots is noise. The precision and direction
     gains are far larger than that.

8. **Stalls are unchanged.**
   - **Count:** both Luna runs had 8 calls that hit the 120 s ReadTimeout and
     succeeded on their retry.
   - **Timing:** they ran at the same time, so the stalls are the provider's,
     not the prompt's.

## Recommendation

Ship prompt v2 with Luna at `low`. Once the PR is merged, deploy it to
staging and restart the 7-day canary (task 09927577) on the combination that
will go to prod (Dave, 2026-11-18). The readout should show:

- no `parse_error` rows;
- edges per call near 0.6 at staging's traffic;
- prompt tokens near 1,650 per call.
