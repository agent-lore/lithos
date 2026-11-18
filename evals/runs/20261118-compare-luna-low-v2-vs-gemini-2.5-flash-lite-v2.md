| metric | 20261118T131246Z-openai_gpt-6-luna-low | 20261118T130351Z-google_gemini-2.5-flash-lite |
|---|---:|---:|
| calls | 328 | 328 |
| labels.relation_agreement | 0.876 | 0.792 |
| labels.direction_agreement | 0.545 | 0.318 |
| reference.relation_agreement | 0.339 | 0.355 |
| reference.direction_agreement | 0.121 | 0.066 |
| contract_violation_rate | 0.000 | 0.005 |
| rate.ok | 1.000 | 0.991 |
| rate.partial | 0.000 | 0.006 |
| rate.empty | 0.000 | 0.000 |
| rate.parse_error | 0.000 | 0.003 |
| rate.error | 0.000 | 0.000 |
| edges_per_call_at_floor | 0.613 | 1.204 |
| tokens.prompt_mean | 1655.1 | 1761.8 |
| tokens.completion_mean | 357.4 | 268.9 |
| tokens.reasoning_share | 0.422 | 0.000 |
| latency_ms.p50 | 4559.6 | 1131.9 |
| latency_ms.p95 | 8289.4 | 1485.2 |
| cost_per_1k_calls | 0.344 | 0.284 |

Evidence: same cases, labels and coverage

Gate (labels basis): **NOT PROMOTABLE**
- FAIL: relation_agreement — 79.2% vs 87.6% (max drop 3%, basis labels)
- FAIL: direction_agreement — 31.8% vs 54.5% (max drop 5%)
- FAIL: rate:partial — 0.6% vs 0.0%
- pass: rate:empty — 0.0% vs 0.0%
- FAIL: rate:parse_error — 0.3% vs 0.0%
- pass: rate:error — 0.0% vs 0.0%
- FAIL: cost_per_1k_calls — $0.2837 vs $0.3442 per 1k calls (18% cheaper; need ≥30% cheaper, no dearer with higher agreement, or ≤25% dearer with relation +3 and direction +5 points)
