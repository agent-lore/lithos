# LLM eval suite

Status: **PRD draft, 2026-09-29** (Dave: "now that Lithos depends on LLM
output and will depend on more, we need a way to validate changes and explore
cost reductions by model choice"). Separate from
`lcma-connected-knowledge.md` K7, which measures whether the memory layer helps
a caller; this measures whether a given model, prompt and budget produce
acceptable artefacts at what cost. Referenced from K7 and from Epic B's
ADR-0009. Precedent for the harness shape: `scripts/calibrate_salience.py`.

Decisions taken with the draft: local models are desirable but **not a hard
goal** for now; eval fixtures **may contain snippets of third-party text**
(influx articles), kept in the repo.

## 1. Problem

Lithos already writes LLM output into its authoritative stores: 5,907
inferred typed edges in production, adjudicated by `openai/gpt-5-mini` via
OpenRouter at roughly 94k tokens a day. Every planned phase-4 artefact
(compiled entity and concept pages, analogy frames, digest items, gated
priming briefs) adds another LLM-produced artefact type.

Nothing validates a change today. The WS1 rollout found by hand-reading
staging probes that `gemini-2.5-flash-lite` produced perfect mechanics with a
constant direction on 55 of 55 judgements, and that `gpt-5-mini` failed 20 of
23 calls until the output cap was raised. A prompt edit, an
`INFERENCE_VERSION` bump, a model swap or a cheaper provider would be judged
the same way, or not at all. Cost exploration is blocked for the same reason:
there is no number that says a cheaper model is good enough.

Reproducibility is also missing: `edge_inference_log` records
`(node_id, doc_version, inference_version, model, edges_written, inferred_at)`
but not the candidate set or snippets the model saw, so a past judgement
cannot be replayed against a new model.

## 2. Goals

1. Any change to a model, prompt, parser, or LLM config knob can be scored
   against a fixed golden set before it reaches staging.
2. Model choice is a measured trade: agreement, failure rates, tokens,
   latency and cost per thousand calls reported together, with a written
   promotion rule.
3. The same harness runs against any OpenAI-compatible endpoint, hosted or
   local (Ollama, vLLM, llama.cpp), so a local model can be tried without
   new code.
4. New artefact types get a golden set as part of their own workstream; the
   harness is generic over "input fixture → model → parsed artefact → scorer".

Non-goals: evaluating retrieval quality (K7); training or fine-tuning; a
hosted eval service; benchmarking against public leaderboards.

## 3. Requirements

### E1 — Replayable inputs (prerequisite, smallest slice)

The engine persists what the model saw, verbatim. Each call records the
**exact prompt messages sent** (system + user, after snippet truncation) and
the raw completion, plus the metadata needed to index them: focus note id
and content hash, ordered candidate ids with similarity scores, prompt
template version, model, token usage and latency. Snippets are stored as
immutable content-addressed blobs (`$LITHOS_DATA_DIR/.lithos/llm-calls/
blobs/<sha256>`) referenced from an `llm_call_log` table in `stats.db`, so a
later note edit cannot change what a past call saw and identical snippets
are stored once. Retained for a configurable window (default 30 days),
exportable as fixtures. Hashes alone are not enough: once a note changes
they cannot reconstruct the input (review comment on PR #431).

### E2 — Golden sets

One directory per artefact type under `evals/<artefact>/`, in the repo:

- `cases.jsonl` — inputs: for typed-edge adjudication, a focus snippet plus
  K candidate snippets (third-party text allowed), all ids and hashes.
- `labels.jsonl` — human labels: relation (or `none`), direction, confidence
  bucket, and a yes/no on whether the rationale is acceptable; labeller and
  date. Symmetric relations (`contradicts`, `analogy_to`) carry no direction.
- `README.md` — how the set was sampled and how labels were produced.

First set: **typed-edge adjudication**, 200–300 cases sampled from
production runs stratified by relation, confidence bucket and namespace,
plus 30 deliberate negatives (near-duplicates, unrelated pairs, same-source
pairs) and 20 non-English pairs. Labelled by Dave with a Claude Code session
proposing labels for review, never accepting them unread. Later sets follow
the phase-4 workstreams: compiled pages (K3), analogy frames (K5), digest
items (K4), priming briefs (K5).

### E3 — Runner

`lithos eval run --artefact edges --model <name> --base-url <url> [--cases N]
[--json]`, implemented in `lithos/eval/` behind the `lithos.lcma` seam
(reuses `LlmClient`, `build_adjudication_prompt`, `parse_adjudications`;
never imports engine internals that write to stores). Runs are recorded as
`evals/runs/<timestamp>-<model>.json` and are comparable with `lithos eval
compare <run-a> <run-b>`.

Per-run metrics, always reported together:

| Metric | Source |
|---|---|
| Relation agreement, direction agreement (directed relations only), rationale acceptability | labels |
| Contract-violation rate, parse-failure rate, partial-judgement rate, empty-judgement rate | parser outcomes |
| Confidence calibration: agreement by model-confidence bucket | labels × output |
| Edges that would be written per call at the current confidence floor | engine policy |
| Prompt and completion tokens, wall latency p50/p95, cost per 1,000 calls (price table in `evals/prices.toml`) | `LlmResult.usage`, timing |

### E4 — Shadow mode on staging

A second `LlmConfig` (`lcma.llm_shadow`) runs beside the live one on the same
drains: identical inputs, writes nothing, records its judgements to the E1
log tagged `shadow=true`. `lithos eval shadow-report --since 7d` diffs shadow
against live on the same calls. This is how a cheaper model earns promotion on
real traffic without a production experiment. Budget-capped separately.

### E5 — Promotion rule

Written into `evals/POLICY.md` and enforced by `lithos eval compare
--gate`: a candidate model or prompt is promotable when relation agreement is
within 3 points of the incumbent, direction agreement within 5, every failure
rate at or below the incumbent's, and cost per 1,000 calls at least 30%
lower (or equal cost with agreement higher). Anything else is a documented
"no" with the numbers. The rule is the deliverable; the thresholds are the
first values, revisable in the same file.

### E6 — Judge hygiene

Human labels are the ground truth for relation and direction. An LLM judge
may score rationale acceptability only; it must be a different and stronger
model than the one under test, its prompt lives in `evals/judges/`, and it is
probed with the deliberate-negative cases (a wrong-but-topical rationale must
be rejected). Runs report the judge's agreement with the human rationale
labels alongside everything else.

### E7 — CI

`make eval-smoke` runs the parser and scorer on the fixtures with a recorded
completion (no network) so parser changes are gated; full model runs are
manual or scheduled, never in the PR gate.

## 4. Open questions

- E1's retention window and blob-store size at ~45 calls/day (the 30-day
  default is a guess; measure after the first week).
- Whether the confidence floor (0.6) should be tuned per model from E3's
  calibration table rather than fixed.
- Who labels the later artefact types when they are prose (compiled pages,
  digests): pairwise preference labels are cheaper than absolute scores.

## 5. Sequence

E1 first (small, unblocks fixtures from live traffic) → E2 first golden set
during Epic A → E3 runner and E5 policy together → E4 shadow on staging →
E6/E7. K3 (compiled pages) does not start generating until E3 exists and the
pages golden set has a sampling plan. Tracked as its own epic in `lithos-core`.

## 6. Success criteria

- A model swap is decided from a `lithos eval compare` report, not a probe.
- At least one cheaper model has been measured against the incumbent on the
  edges set and on staging shadow traffic, with the result recorded either way.
- Every LLM artefact type in production has a golden set before it ships.
