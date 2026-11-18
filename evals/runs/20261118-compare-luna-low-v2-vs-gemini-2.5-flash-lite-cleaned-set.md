| metric | 20261118T163856Z-openai_gpt-6-luna-low | 20261118T164907Z-google_gemini-2.5-flash-lite |
|---|---:|---:|
| calls | 328 | 328 |
| labels.relation_agreement | 0.861 | 0.644 |
| labels.direction_agreement | 0.523 | 0.136 |
| reference.relation_agreement | 0.339 | 0.273 |
| reference.direction_agreement | 0.106 | 0.048 |
| contract_violation_rate | 0.000 | 0.006 |
| rate.ok | 1.000 | 0.738 |
| rate.partial | 0.000 | 0.006 |
| rate.empty | 0.000 | 0.000 |
| rate.parse_error | 0.000 | 0.003 |
| rate.error | 0.000 | 0.253 |
| edges_per_call_at_floor | 0.616 | 0.875 |
| tokens.prompt_mean | 1647.9 | 1778.4 |
| tokens.completion_mean | 365.0 | 291.3 |
| tokens.reasoning_share | 0.438 | 0.000 |
| latency_ms.p50 | 4613.9 | 1059.1 |
| latency_ms.p95 | 7619.9 | 1488.5 |
| cost_per_1k_calls | 0.347 | 0.294 |

Evidence: same cases, labels and coverage

Gate (labels basis): **NOT PROMOTABLE**
- FAIL: relation_agreement — 64.4% vs 86.1% (max drop 3%, basis labels)
- FAIL: direction_agreement — 13.6% vs 52.3% (max drop 5%)
- FAIL: rate:partial — 0.6% vs 0.0%
- pass: rate:empty — 0.0% vs 0.0%
- FAIL: rate:parse_error — 0.3% vs 0.0%
- FAIL: rate:error — 25.3% vs 0.0%
- FAIL: cost_per_1k_calls — $0.2944 vs $0.3473 per 1k calls (15% cheaper; need ≥30% cheaper, no dearer with higher agreement, or ≤25% dearer with relation +3 and direction +5 points)
