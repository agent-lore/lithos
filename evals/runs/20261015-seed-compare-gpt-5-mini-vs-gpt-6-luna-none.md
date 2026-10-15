# Seed-set comparison: gpt-5-mini (incumbent) vs gpt-6-luna, reasoning_effort=none

First end-to-end run of the harness on OpenRouter, 2026-10-15, on the
**six synthetic seed cases** (11 labelled slots, 4 directed). This proves the
runner, scorer and gate work against the real endpoint; it is far too small
to decide the migration. The decision run uses the production export
(≥250 cases, ~100 reviewed labels; task 4044f201).

Re-scored after the PR #451 review: direction agreement is now measured over
the ground truth's directed slots, so a missed relation on a directed slot
counts as a missed direction too.

What the seed set already shows: gpt-5-mini spends 83% of its completion
tokens on hidden reasoning (755 completion tokens per call vs Luna's 89),
which is where the 20x cost gap comes from at this prompt size.

| metric | 20261015T154746Z-openai_gpt-5-mini | 20261015T154754Z-openai_gpt-6-luna-none |
|---|---:|---:|
| calls | 6 | 6 |
| labels.relation_agreement | 0.818 | 0.818 |
| labels.direction_agreement | 0.500 | 0.750 |
| reference.relation_agreement | 0.818 | 0.909 |
| reference.direction_agreement | 0.500 | 1.000 |
| contract_violation_rate | 0.000 | 0.000 |
| rate.ok | 1.000 | 1.000 |
| rate.partial | 0.000 | 0.000 |
| rate.empty | 0.000 | 0.000 |
| rate.parse_error | 0.000 | 0.000 |
| rate.error | 0.000 | 0.000 |
| edges_per_call_at_floor | 1.167 | 1.167 |
| tokens.prompt_mean | 363.0 | 363.0 |
| tokens.completion_mean | 755.3 | 89.3 |
| tokens.reasoning_share | 0.833 | 0.000 |
| latency_ms.p50 | 6240.9 | 2062.4 |
| latency_ms.p95 | 9658.5 | 2353.1 |
| cost_per_1k_calls | 1.601 | 0.081 |

Evidence: same cases, labels and coverage

Gate (labels basis): **PROMOTABLE**
- pass: relation_agreement — 81.8% vs 81.8% (max drop 3%, basis labels)
- pass: direction_agreement — 75.0% vs 50.0% (max drop 5%)
- pass: rate:partial — 0.0% vs 0.0%
- pass: rate:empty — 0.0% vs 0.0%
- pass: rate:parse_error — 0.0% vs 0.0%
- pass: rate:error — 0.0% vs 0.0%
- pass: cost_per_1k_calls — $0.0810 vs $1.6014 per 1k calls (95% cheaper; need ≥30% cheaper, or no dearer with higher agreement)
