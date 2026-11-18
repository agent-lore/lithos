# Promotion rule for LLM artefact models and prompts

A candidate (a different model, reasoning setting, provider, or prompt
version) may replace the incumbent for an artefact when `lithos eval compare
<incumbent-run> <candidate-run> --gate` passes on the same golden set. The
rule and its thresholds live here; `--gate` reads the `toml` block below, so
revising a number is a reviewed edit to this file.

The incumbent for typed-edge adjudication is `openai/gpt-5-mini` on
OpenRouter (prompt version 1).

## Rule

All of the following, measured on the same cases:

1. **Relation agreement** with the ground truth is within
   `relation_agreement_max_drop` of the incumbent's.
2. **Direction agreement** is within `direction_agreement_max_drop` of the
   incumbent's. The denominator is the ground truth's directed slots (a
   missed relation on a directed slot is a missed direction too), so a
   candidate cannot empty the measurement by answering `none`; the check is
   skipped only when the truth has no directed slots at all. A label whose
   direction is `either` (the prompt does not show the order) scores its
   relation only and is left out of this denominator.

A drop of exactly a maximum passes, whatever the float rounding.
3. **Every failure rate** in `failure_outcomes` (`partial`, `empty`,
   `parse_error`, `error`) is at or below the incumbent's.
4. **Cost per 1,000 calls** (from `evals/prices.toml` at the run's mean token
   split) meets one of these:
   - it is at least `cost_min_reduction` lower;
   - it is no higher, with relation agreement strictly better;
   - **quality path:** it is at most `quality_max_cost_increase` higher, and
     agreement rose by at least each measure's tolerance: relation by
     `relation_agreement_max_drop`, and direction by
     `direction_agreement_max_drop` when the truth has directed slots.

   The first two suit a model swap, whose point is a lower cost. The quality
   path lets a prompt change that is clearly better pay a bounded price. It
   was added for prompt v2 (2026-11-18), which was 14% dearer than v1 and
   raised relation agreement by 9 points and direction agreement by 30.

Ground truth is the reviewed human labels when both runs were scored against
them (`labels.n_slots > 0`); otherwise the incumbent's own recorded
judgements (`reference`), which is a weaker basis and the report says so.

Both runs must have scored the **same evidence**: identical case contents,
identical reviewed labels, and a result for every case. Each run document
carries fingerprints of these; `--gate` refuses to give a verdict when they
differ (a `--limit` trial, a revised label set or a re-exported golden set
is not comparable with a full run).

Anything else is a documented "no" with the numbers: commit the comparison
under `evals/runs/` either way.

```toml
relation_agreement_max_drop = 0.03
direction_agreement_max_drop = 0.05
cost_min_reduction = 0.30
quality_max_cost_increase = 0.25
failure_outcomes = ["partial", "empty", "parse_error", "error"]
```

## Not covered

Latency has no threshold: adjudication runs in a background worker, so it is
reported for information only. Rationale quality is reported from the
`reference_rationale_ok` labels on the incumbent; a candidate's rationales
are judged only once the E6 judge exists.
