# Typed-edge adjudication golden set

Artefact: the relation/direction/confidence judgements the enrich worker
asks the LLM for in `lithos.lcma.edge_inference` (prompt version 2 since
2026-11-18; see [Prompt v2 and labels v2](#prompt-v2-and-labels-v2-2026-11-18)).
Procedure: `docs/runbooks/llm-model-migration.md` steps 2–3.

## Files

| File | What |
|---|---|
| `cases.jsonl` | The production set: one case per line with the focus note, its numbered candidates (v1 text exactly as sent, and since v2 each note's `note` view: date, source, feed and summary text), the verbatim messages when exported from the call log, and the incumbent's judgements as `reference`. |
| `labels.jsonl` | Human labels, one per `(case_id, candidate_index)`: `relation` (or `none`), `direction` for directed relations only (`either` when the prompt text does not show which note came first), labeller `confidence`, whether the incumbent's rationale was acceptable, labeller and date. Only `status: reviewed` rows are scored. v2 covers 202 slots: the 181 of [Labels v1](#labels-v1-2026-11-16), re-checked, and 21 mirrors. |
| `mirror-exclude.txt` | Labelled slots that `scripts/eval_cases.py mirror` must not mirror, each with its reason. |
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
| Hand-built negatives (25) and near-duplicates (5) | +30 |
| Hand-built French-titled cases | +6 |
| **Total** | **307 cases, 1,402 slots** |

No further stratified down-sampling: 271 is inside the 200–300 target, and the
labelled subset (step 3) is where stratification matters.

**Incumbent's judgements on the 1,341 exported slots.** These are the
incumbent's answers, not ground truth; 120 of these slots carry human labels
(see Labels v1).

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
  lists. The labels confirm it: of the 21 labelled slots where the incumbent
  said `analogy_to`, the reviewed label agrees on 2.
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
| `positive:near_duplicate` | 5 | 5 | The same item as a different note: LessWrong/Alignment Forum crossposts, the same release reported by two outlets, a forum thread and its `Re:` reply | `supports` (convention 1 below; tagged `negative:` until the labels settled it) |
| `negative:same_source` | 12 | 18 | Same feed, different content: five recurring upload/installer lists for different weeks, and seven same-feed posts on different topics | `none` |
| `negative:unrelated` | 13 | 26 | Focus and candidates from different domains (retro computing, AI alignment, robotics, C64, SGI) | `none` |
| `lang:fr` (`fr-*`) | 6 | 12 | French-titled `amiga-impact` notes, with their top two `influx` neighbours from a prod semantic search run the way the engine queries | labelled in review: `none` 6, `refines` 4, `supports` 2 |

**Non-English.** The corpus has no non-English text at the prompt level.
The 12 French notes are `amiga-impact` items whose influx summary is in
English, so only the title is French. Besides the six `fr-*` cases, eight
exported cases include one of those notes as a candidate and carry
`lang:fr`, giving 14 `lang:fr` cases and about 20 French-titled pairs.

## Labels v1 (2026-11-16)

Task 4044f201. `labels.jsonl` holds 181 reviewed slots across 60 cases,
labelled by Dave. They come from two groups:

- **24 exported cases (120 slots).** They were picked greedily to cover
  every incumbent relation, both environments and 16 feeds, with at most one
  case per article.
- **Every hand-built slot:** 36 cases, 61 slots.

The other 247 exported cases are scored against the incumbent's reference
only.

**Method.**

1. Claude proposed a label for every slot, blind: it did not see the
   reference. The proposals went on a review sheet made with
   `scripts/eval_labels.py sheet`.
2. Dave set the labelling standard and five conventions on two calibration
   cases.
3. Dave labelled 18 slots blind: 12 exported and 6 hand-built, one per case.
   That sheet showed neither the reference nor the proposals, and he
   labelled it before reading the main sheet.
4. Claude revised its proposals to the conventions. Dave then reviewed every
   slot with the reference and the proposal in view and changed 8 of them.
   `scripts/eval_labels.py import` wrote this file.

The review sheets, the proposals and the blind labels are local working
files and are not committed. A note that starts with `claude` is Claude's
reason for a proposal, which Dave kept. `claude (v2, conv N):` marks a
proposal revised to convention N. Every other note is Dave's.

**Standard: strict.** A relation is named only when the prompt's definition
literally holds. A shared topic, venue or source alone is `none`, as the
prompt says: "use this freely; most pairs are unrelated". The incumbent reads
the definitions more loosely, and that is most of the gap below.

**Conventions.** For 1–3, A is the newer or derivative note.

1. **Near-duplicates** (the same item as another note, including
   cross-lingual relays): `supports`, even when one copy adds detail. A
   relay, crosspost or later report supports the original.
2. **Successive releases or issues of the same thing** (alpha N vs N−1,
   magazine issue 3 vs 2): `refines`, with the newer one as A.
3. **A review or interview about an item:** `refines`, with the review or
   interview as A.
4. **Same research thread, different finding:** `none`, unless one finding
   is evidence for the other's claim.
5. **Same recurring series, different content** (weekly upload lists,
   newsletter posts, product changelog entries): `none`.

Notes that share only a subject are `none`, for example several events at
the same museum. `analogy_to` needs different domains. Whether the
vocabulary should gain a same-subject relation is an open question; it is not
a labelling question.

**Directions come from the prompt text only.** The model sees each
candidate's title and one search chunk of at most 700 characters, not its
summary, and only 143 of the 1,402 candidates show their date or source. A
direction is scored only when the prompt itself shows the order: dates on
both notes, an issue number, a `Re:`, "relayed by", "review of", or the
content (one note is evidence for, or an instance of, the other). Four
near-duplicate slots fail that test, so their direction is `either`: the
relation must still be `supports`, and the direction is not scored (PR #460
review).

- `fr-06/1`, `neg-dup-01/1`, `neg-dup-05/1`: the candidate shows neither its
  date nor its source.
- `neg-dup-03/1`: both copies are dated the same day.

`b97304a3/1` stays scored: the focus title says "New alpha version".

The v1 review sheets cut each text at 600 characters, so on 47 of the 60
focus notes and 68 of the 181 candidates the labeller saw up to about 100
fewer characters than the model did; the missing text was the tail of the
Result or Relevance section. The sheet now quotes exactly what the model
sees.

**The labels.**

| Stratum | Slots | none | supports | refines | analogy_to | is_example_of | contradicts |
|---|---|---|---|---|---|---|---|
| Exported | 120 | 102 | 9 | 2 | 3 | 2 | 2 |
| `positive:near_duplicate` | 5 | 0 | 5 | 0 | 0 | 0 | 0 |
| `negative:same_source` | 18 | 18 | 0 | 0 | 0 | 0 | 0 |
| `negative:unrelated` | 26 | 26 | 0 | 0 | 0 | 0 | 0 |
| `lang:fr` (`fr-*`) | 12 | 6 | 2 | 4 | 0 | 0 | 0 |
| **Total** | **181** | **152** | **16** | **6** | **3** | **2** | **2** |

- 24 slots name a directed relation: 20 have a scored direction and 4 are
  `either`.
- Confidence: high 106, medium 50, low 25.
- Two slots record `rationale_ok`.
- No slot is labelled `depends_on`, so this set cannot score that relation.

**The incumbent against the labels.** These figures come from gpt-5-mini's
recorded answers on the 120 exported slots. They are a preview; the gate's
baseline is the incumbent's run in runbook step 4.

- It agrees on the relation for 32 of 120 slots (26.7%), and on direction for
  7 of 13 (53.8%).
- It named a relation on 84 of the 102 slots labelled `none`.

| Incumbent said | Slots | Label agrees |
|---|---|---|
| `supports` | 32 | 8 |
| `analogy_to` | 21 | 2 |
| `refines` | 19 | 1 |
| `none` | 18 | 18 |
| `is_example_of` | 13 | 1 |
| `depends_on` | 11 | 0 |
| `contradicts` | 6 | 2 |

**Agreement.** 18 double-labelled slots is a small sample: these figures
show large disagreements, not fine ones.

- **One labeller, two passes** (Dave's blind label vs his reviewed label):
  - On the 14 slots not discussed between the passes, the relation matches
    on all 14, and relation and direction together on 13.
  - The other 4 slots were discussed when convention 1 was clarified, so they
    are not counted.
- **Two labellers** (Dave's blind labels vs Claude's original blind
  proposals; neither saw the other's answers or the reference):
  - The relation matches on 15 of 18, and relation and direction together on
    14.
  - All 3 relation disagreements are near-duplicate or successive-release
    slots, which the conventions now settle.
  - On the 12 exported slots among them, the incumbent's recorded answer
    matches Dave's blind relation on 6.
- **Review against the proposals:**
  - Dave changed 8 of the 181 proposed labels: 2 relations and 6 confidences.
  - Against Claude's original blind proposals, 13 relations differ. 11 come
    from the conventions; on the other 2, Dave adopted the incumbent's answer.

**Reading the gate on these labels** (`evals/POLICY.md`):

- **The relation check is lenient here.** Under the strict standard the
  incumbent's own relation agreement is low, so a candidate that links less
  passes it easily.
- **Answering `none` everywhere still fails.** Direction agreement is
  measured over the 20 truth slots with a scored direction, and such a
  candidate gets every one of them wrong.
- **The direction check is coarse.** Each scored direction is worth 5 points
  of direction agreement, so the 5-point tolerance is exactly one slot.
  Read a narrow direction failure from the per-slot results before acting on
  it.

## Prompt v2 and labels v2 (2026-11-18)

Task a06c08c0. Prompt v2 (`INFERENCE_VERSION` 2,
`lithos.lcma.note_view`) shows every note the same way: an
`Added <date> · Source: <domain> (<feed>)` line, the summary's Contributions
and Result, Method when it fits whole, and for a candidate the matching search
chunk when room remains. Each note gets up to 1,200 characters
(`snippet_chars`). The system prompt now states the strict standard and
conventions 1, 2, 3 and 5 below, with one worked example per direction.

**Cases.** `scripts/eval_cases.py attach-notes` read every focus and
candidate note from the vault its case was exported from (prod 178 cases,
staging 129) and stored its view as `note`. All 1,709 notes were found. Two
of the 271 exported focus notes have changed since export; `note_versions`
records the version read. The v1 fields (`focus.content`,
`candidates[].snippet`) stay exactly as sent, so a v1 run of the same file
still renders what v1 saw: exported cases replay their recorded messages, and
hand-built ones were always rebuilt from those fields. `snippet_chars` is 1200
in every case; v1 text was already cut at 700, so the budget only changes v2.

**Mirrors.** The labels' scored directions leaned `focus_to_candidate` (16 of
20), because the focus is usually the newer note, so a model that always
answers that way scored well. `scripts/eval_cases.py mirror` adds one case per
label with a scored direction: the two notes swapped, the direction flipped,
the same relation and confidence (`mirror-*`, tagged `positive:mirror`). A
mirrored focus shows the note's first 700 characters to v1, and the mirrored
candidate's chunk is the original focus text v1 showed. Two slots are not
mirrored (`mirror-exclude.txt`): their evidence is only in the candidate's
passage, and a focus never shows one.

**Labels re-checked.** Every v1 label rested on what v1 showed, so all 201
labelled slots (181 plus the first 20 mirrors) were re-read in the v2 text:

1. Four reviewers (Claude subagents, one per quarter of the sheet, read-only)
   proposed a change only where the v2 text changes the answer: 6 changes and
   14 doubts.
2. Claude checked each against the sheet.
3. Dave decided:

| Slot | v1 | v2 | Why |
|---|---|---|---|
| `neg-dup-01/1` | `supports either` | `supports focus_to_candidate` | Dates now shown: the LessWrong crosspost was added a day after the Alignment Forum original |
| `9391c1906304d239/1` | `none` | `supports candidate_to_focus` | The summary shows toy-model evidence for the focus's "dependence on strong initialization" |
| `0bfb6a29b244d3f0/5` | `none` | `supports focus_to_candidate` | The summary shows the same filler-token finding as candidate 4 |
| `9391c1906304d239/2` | `supports focus_to_candidate` | `supports candidate_to_focus` | The focus is "a hypothesis—not an empirical test"; the candidate presents evidence |
| `f58eaca71093f324/5` | `analogy_to` | `none` | The summary shows a coding-tools note, so only the topic is shared |
| `neg-dup-03/1` | `supports either` | unchanged | The Added dates reflect ingestion; the forum thread relays the video |

The two mirrors the reviewers rejected are the excluded ones above. Two of
the doubts became the last two changes in the table. Claude kept the other 12
doubts as labelled. In most of them the reviewer judged that the label still
holds, or questioned only its confidence, which is not scored. Two remain open
for a later pass:

- `9d829de53d5096ac/3`: Bryan Cantrill's blog post reflecting on the Solaris
  layoffs. It could be `supports` as labelled, or `refines` under convention 3.
- `fr-03/1`: the shown text links the French relay to the datagubbe.se
  primer only by topic and "relayed by OSNews".

**Labels v2.**

| | Original slots | Mirrors | Total |
|---|---|---|---|
| Slots | 181 | 21 | 202 |
| Positive | 30 | 21 | 51 |
| Scored `focus_to_candidate` | 17 | 6 | 23 |
| Scored `candidate_to_focus` | 6 | 15 | 21 |
| `either` | 3 | 0 | 3 |

Each scored direction is now worth about 2.3 points of direction agreement,
so the gate's 5-point tolerance is two slots, and a constant-direction model
gets about half of them wrong.

**Comparing prompts.** Run v1 with the code before this change (main at
f9b884d) and v2 with the current code, both on this `cases.jsonl` and
`labels.jsonl`, so the case and label fingerprints match.
