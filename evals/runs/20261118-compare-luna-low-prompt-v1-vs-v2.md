| metric | 20261118T131316Z-openai_gpt-6-luna-low | 20261118T131246Z-openai_gpt-6-luna-low |
|---|---:|---:|
| calls | 328 | 328 |
| labels.relation_agreement | 0.787 | 0.876 |
| labels.direction_agreement | 0.250 | 0.545 |
| reference.relation_agreement | 0.361 | 0.339 |
| reference.direction_agreement | 0.126 | 0.121 |
| contract_violation_rate | 0.000 | 0.000 |
| rate.ok | 1.000 | 1.000 |
| rate.partial | 0.000 | 0.000 |
| rate.empty | 0.000 | 0.000 |
| rate.parse_error | 0.000 | 0.000 |
| rate.error | 0.000 | 0.000 |
| edges_per_call_at_floor | 0.829 | 0.613 |
| tokens.prompt_mean | 986.4 | 1655.1 |
| tokens.completion_mean | 407.6 | 357.4 |
| tokens.reasoning_share | 0.488 | 0.422 |
| latency_ms.p50 | 4970.0 | 4559.6 |
| latency_ms.p95 | 7769.3 | 8289.4 |
| cost_per_1k_calls | 0.302 | 0.344 |

Evidence: same cases, labels and coverage

Gate (labels basis): **PROMOTABLE**
- pass: relation_agreement — 87.6% vs 78.7% (max drop 3%, basis labels)
- pass: direction_agreement — 54.5% vs 25.0% (max drop 5%)
- pass: rate:partial — 0.0% vs 0.0%
- pass: rate:empty — 0.0% vs 0.0%
- pass: rate:parse_error — 0.0% vs 0.0%
- pass: rate:error — 0.0% vs 0.0%
- pass: cost_per_1k_calls — $0.3442 vs $0.3024 per 1k calls (14% dearer; need ≥30% cheaper, no dearer with higher agreement, or ≤25% dearer with relation +3 and direction +5 points; passes on the quality path)
