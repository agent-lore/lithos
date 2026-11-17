| metric | 20261117T135250Z-openai_gpt-5-mini | 20261117T132152Z-openai_gpt-6-luna-none |
|---|---:|---:|
| calls | 307 | 307 |
| labels.relation_agreement | 0.420 | 0.845 |
| labels.direction_agreement | 0.100 | 0.050 |
| reference.relation_agreement | 0.667 | 0.315 |
| reference.direction_agreement | 0.538 | 0.067 |
| contract_violation_rate | 0.005 | 0.000 |
| rate.ok | 0.993 | 0.990 |
| rate.partial | 0.007 | 0.000 |
| rate.empty | 0.000 | 0.000 |
| rate.parse_error | 0.000 | 0.010 |
| rate.error | 0.000 | 0.000 |
| edges_per_call_at_floor | 3.156 | 0.469 |
| tokens.prompt_mean | 1013.4 | 1013.9 |
| tokens.completion_mean | 1252.8 | 225.4 |
| tokens.reasoning_share | 0.788 | 0.000 |
| latency_ms.p50 | 16510.5 | 2821.5 |
| latency_ms.p95 | 142970.2 | 4010.0 |
| cost_per_1k_calls | 2.759 | 0.214 |

Evidence: same cases, labels and coverage

Gate (labels basis): **NOT PROMOTABLE**
- pass: relation_agreement — 84.5% vs 42.0% (max drop 3%, basis labels)
- pass: direction_agreement — 5.0% vs 10.0% (max drop 5%)
- pass: rate:partial — 0.0% vs 0.7%
- pass: rate:empty — 0.0% vs 0.0%
- FAIL: rate:parse_error — 1.0% vs 0.0%
- pass: rate:error — 0.0% vs 0.0%
- pass: cost_per_1k_calls — $0.2141 vs $2.7589 per 1k calls (92% cheaper; need ≥30% cheaper, or no dearer with higher agreement)
