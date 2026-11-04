# Runbook: evaluating and migrating the adjudication LLM

How to decide whether a different model, provider, reasoning setting or prompt
version may replace the one Lithos uses for typed-edge adjudication, and how
to roll it out. The same procedure covers a hosted swap (e.g. `gpt-5-mini`
→ `gpt-6-luna`) and an on-prem trial (Ollama, vLLM, llama.cpp `/v1`).

Design: `docs/plans/llm-eval-suite.md`. Commands: `docs/cli.md` (`eval`).
Rule: `evals/POLICY.md`. Fixture format: `evals/edges/README.md`.

## Roles and timeline

| Step | Who | When | Takes |
|---|---|---|---|
| 0. Prerequisites | operator | once | 10 min |
| 1. Let the call log fill on staging | nobody | after the log ships, continuously | ≥ 1 week of traffic |
| 2. Export and shape the golden set | agent | when ≥ 250 usable calls exist | 1 h |
| 3. Label a reviewed subset | agent proposes, **human reviews** | after step 2 | 1–2 h human time |
| 4. Run the incumbent and candidates, gate | agent | after step 3 (or step 2 for an early, weaker signal) | 15 min, pennies |
| 5. Cut staging over, canary | operator | after a PROMOTABLE verdict | 7 days wall clock |
| 6. Cut prod over | operator, with Dave's go | after a clean canary | 10 min |

Steps 2–4 repeat for every later candidate; the golden set and labels are
reused until the prompt version or the corpus changes materially.

## 0. Prerequisites

- **Credits.** The provider account must have credit. An exhausted OpenRouter
  key returns `402 Insufficient credits`; every call then scores `error`, the
  staging worker logs `LLM adjudication failed … 402`, and no usable
  fixtures accumulate. Check before a run and before relying on the log.
- **Key in the environment, never on the command line.** `lithos eval run`
  reads the key from the variable named by `--api-key-env` (default
  `OPENROUTER_API_KEY`). For a local endpoint with no key pass
  `--api-key-env NONE`.
- **A checkout with the eval suite** (`main` after PRs #450 and #451) and
  `uv sync`. Run commands from the repo root as `uv run lithos …`; the
  fixture paths below are relative to it.
- **Staging on a build that writes the call log** (#450 or later). The log is
  on whenever `lcma.llm.base_url` is set and
  `lcma.llm.call_log_retention_days` (default 30) is non-zero.
- **The incumbent's exact configuration**, from `docker/.env.<env>`:
  `LITHOS_LCMA__LLM__MODEL`, `…REASONING_EFFORT` (unset = provider default),
  `…MAX_OUTPUT_TOKENS`, `…CONFIDENCE_FLOOR`, `…SNIPPET_CHARS`. The baseline
  run must use the same values or it is not the incumbent.

## 1. Let the call log fill

Every adjudication on staging is recorded verbatim (`stats.db`
`llm_call_log` plus blobs under `.lithos/llm-calls/blobs/`). Nothing to do
but wait and watch:

```bash
docker exec lithos-staging lithos eval log-report --since 7d
```

Move on when the window shows **≥ 250 calls with outcome `ok` or
`partial`** for the incumbent (staging averages ~47 calls/day, so about a
week; a quiet week needs longer or seeded traffic via Influx submissions).
While waiting, note the blob-store size (`du -sh
<staging data>/.lithos/llm-calls/blobs`) against the 30-day retention
default; task 73b074f8 wants that number recorded after the first week.

## 2. Export and shape the golden set

Export from **both** environments once the call log is on in each. The
export is read-only. Running it inside the container avoids opening the live
data directory from the host.

```bash
for c in lithos lithos-staging; do
  docker exec "$c" lithos eval export --since 14d --model openai/gpt-5-mini \
      > /tmp/export-"$c".jsonl
done
```

Case ids are a prefix of the request digest, so identical requests dedupe
within one export. Prod and staging ingest the same feeds with separately
generated summaries, so the same article shows up as two different cases.
Keep both, but take at most one per article into the labelled subset.

Before shaping, **drop every case whose focus note or any candidate is
outside the `influx` namespace**: personal, planning and agent notes. This
repo is public, and the 2026-09-29 decision covers third-party text only. Also
keep only the latest call per focus note.

Then shape `evals/edges/cases.jsonl`:

1. **Stratify** to 200–300 cases by the incumbent's relation (each relation in
   `RELATION_VOCAB` at least 15 times where the traffic allows), confidence
   bucket and namespace. The reference judgements are in each case's
   `reference.judgements`; a case with none is a "none" case and should be a
   large minority, not the majority.
2. **Add deliberate negatives** (about 30): near-duplicate pairs, unrelated
   pairs, same-source pairs, written by hand in the same case format with
   `messages: null`, `prompt_version: null` and a `tags` entry such as
   `negative:near_duplicate`. The runner rebuilds the prompt from the
   structured fields.
3. **Add non-English pairs** (about 20) if the corpus has them, tagged
   `lang:<code>`.
4. Write the result to `evals/edges/cases.jsonl`. The synthetic seed set
   lives in `evals/edges/seed/`, where `make eval-smoke` uses it. Update
   `evals/edges/README.md` (sampling window, filters, counts per stratum, who
   shaped it) and commit. The smoke test also replays every exported case's
   reference, so a malformed export fails `make check`.

Cases may contain snippets of third-party text (decision 2026-09-29).

## 3. Label a reviewed subset

Human labels are the ground truth for relation and direction. The agent
proposes; a human reviews every line; nothing proposed is accepted unread.

```bash
# Pick ~100 slots stratified like step 2 and list their case ids, one per line.
python scripts/eval_labels.py sheet evals/edges/cases.jsonl \
    --case-ids /tmp/subset.txt --out /tmp/review.md
```

The sheet shows the focus, each candidate, the incumbent's judgement and a
pre-filled `label:` line. Edit the lines in place:

```
label: <relation> [focus_to_candidate|candidate_to_focus] [low|medium|high] [rationale_ok=yes|no]  # note
label: skip
```

`none`, `contradicts` and `analogy_to` take no direction. `rationale_ok`
says whether the *incumbent's* rationale was acceptable. Then:

```bash
python scripts/eval_labels.py import /tmp/review.md --labeller dave \
    --out evals/edges/labels.jsonl --merge-into evals/edges/labels.jsonl
```

Double-label about 10% of the slots (a second sheet over the same ids) and
record the agreement in `evals/edges/README.md`. Commit `labels.jsonl`.
Only rows with `status: reviewed` are scored.

## 4. Run and gate

All runs on the **same** `cases.jsonl` and `labels.jsonl`; `--gate` refuses
runs whose evidence fingerprints differ (a `--limit` trial, a relabel, a
re-export). Each full run on 250 cases costs well under a dollar on the
models in `evals/prices.toml`; add a row there for any model not listed or
its cost reports `n/a` and the gate's cost check fails.

```bash
export OPENROUTER_API_KEY=…
# 1. Incumbent, with the deployed settings
uv run lithos eval run --model openai/gpt-5-mini --base-url https://openrouter.ai/api/v1
# 2. Candidate — OpenAI's guidance for a GPT-4.x/5.x-style workload on Luna is effort "none" first
uv run lithos eval run --model openai/gpt-6-luna --base-url https://openrouter.ai/api/v1 --reasoning-effort none
# 3. Only if 2 fails the gate on agreement
uv run lithos eval run --model openai/gpt-6-luna --base-url https://openrouter.ai/api/v1 --reasoning-effort low
# 4. Sanity check: a model with a known failure mode must fail the gate
uv run lithos eval run --model google/gemini-2.5-flash-lite --base-url https://openrouter.ai/api/v1
# 5. A local model: Ollama/vLLM /v1 shim, hidden reasoning off, no key
uv run lithos eval run --model qwen3:30b --base-url http://localhost:11434/v1 \
    --extra-body '{"think": false}' --api-key-env NONE
```

Each run writes `evals/runs/<ts>-<model>[-effort].json` and prints the
headline table. Read it before comparing: `rate.error` at 100% means the
endpoint, not the model (credits, URL, key); `parse_error` or `partial`
above the incumbent's means the model cannot hold the JSON contract;
`tokens.reasoning_share` shows how much of the completion is hidden
reasoning, which is where most of a reasoning model's cost sits.

```bash
uv run lithos eval compare evals/runs/<incumbent>.json evals/runs/<candidate>.json --gate \
    | tee evals/runs/<date>-compare-<incumbent>-vs-<candidate>.md
```

Exit 0 is PROMOTABLE under `evals/POLICY.md`; exit 1 lists the failing
checks. Commit the run files and the report either way, and record the
headline numbers in the Lithos task outcome (epic ee397e6c). The sanity-check
model must come out NOT PROMOTABLE; if it passes, the golden set is not
discriminating enough and needs more directed and negative cases before the
verdict means anything.

## 5. Cut staging over and watch the canary

A PROMOTABLE verdict earns a staging canary, not a prod change.

1. PR: in `docker/.env.staging` set `LITHOS_LCMA__LLM__MODEL` to the
   candidate and `LITHOS_LCMA__LLM__REASONING_EFFORT=none` (or whatever the
   winning run used; `--extra-body` becomes `LITHOS_LCMA__LLM__EXTRA_BODY`).
   Leave `max_output_tokens` generous: reasoning models spend it before they
   emit text.
2. Deploy: `./docker/run.sh staging restart`, then
   `curl -sf localhost:8766/health` and check the log for
   `EnrichWorker started`.
3. Do **not** purge `edge_inference_log`: a model switch deliberately does
   not re-adjudicate the corpus (see `INFERENCE_VERSION` in
   `lithos.lcma.edge_inference`).
4. After 7 days, compare the candidate's week with the incumbent's previous
   weeks on the same instance:

   ```bash
   docker exec lithos-staging lithos eval log-report --since 7d
   docker exec lithos-staging lithos eval log-report --since 35d   # incumbent rows for the prior weeks
   ```

   Clean means: `error` and `parse_error` rates no higher than the
   incumbent's, `partial` not materially higher, `edges/call` in the same
   range (a collapse to ~0 or a jump to `neighbour_k` is a prompt-contract
   problem), tokens/call and latency as the eval predicted. The OTEL
   counters `lithos.lcma.llm.calls{outcome}` and `lithos.lcma.llm.tokens`
   show the same thing on the Grafana side.
5. Rollback is the env change reverted and `./docker/run.sh staging restart`.

## 6. Cut prod over

Same env change in `docker/.env.prod` and `./docker/run.sh prod restart`,
on Dave's go, ideally at a quiet time: the restart is a brief outage for
every connected agent and the host is shared with staging. Watch
`log-report --since 1d` the next day and the OTEL error rate for the first
week. Update `CHANGELOG.md` and the eval-suite plan's status, and close the
Lithos tasks with the numbers.

## Repeating for the next candidate

Reuse the committed golden set and labels; run steps 4–6 only. Re-export
(steps 2–3) when the prompt version bumps, the relation vocabulary changes,
or the corpus has shifted enough that the sampled namespaces no longer
represent the traffic. Shadow mode (eval-suite E4) will let a candidate be
scored on live traffic without the staging cutover; until it exists the
canary is the live test.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Every result `error`, detail `402` | provider credits exhausted | top up; staging inference is failing too |
| `rate.error` 100%, detail `connect`/`timeout` | wrong `--base-url` or endpoint down | check the `/v1` path and the host |
| `cost per 1k calls: n/a` | model id not in `evals/prices.toml` | add a row with the provider's listed prices and the source/date |
| `--gate` says "not comparable" | runs over different cases, labels or coverage | re-run both on the committed set without `--limit` |
| `parse_error` high on one model | model ignores the JSON contract | try `--reasoning-effort none`, a larger `--max-output-tokens`; else it is a "no" |
| direction agreement far below relation agreement | constant-direction failure (seen on gemini-2.5-flash-lite) | it is a "no"; the gate catches it |
| `make eval-smoke` fails after a parser edit | the parser now reads a recorded completion differently | intended; fix the parser or, if the new reading is right, update the pinned expectation and say why |
