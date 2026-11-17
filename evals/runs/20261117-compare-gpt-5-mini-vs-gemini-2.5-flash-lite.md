| metric | 20261117T135250Z-openai_gpt-5-mini | 20261117T131749Z-google_gemini-2.5-flash-lite |
|---|---:|---:|
| calls | 307 | 307 |
| labels.relation_agreement | 0.420 | 0.735 |
| labels.direction_agreement | 0.100 | 0.250 |
| reference.relation_agreement | 0.667 | 0.312 |
| reference.direction_agreement | 0.538 | 0.061 |
| contract_violation_rate | 0.005 | 0.005 |
| rate.ok | 0.993 | 0.909 |
| rate.partial | 0.007 | 0.010 |
| rate.empty | 0.000 | 0.000 |
| rate.parse_error | 0.000 | 0.010 |
| rate.error | 0.000 | 0.072 |
| edges_per_call_at_floor | 3.156 | 1.169 |
| tokens.prompt_mean | 1013.4 | 1036.0 |
| tokens.completion_mean | 1252.8 | 280.4 |
| tokens.reasoning_share | 0.788 | 0.000 |
| latency_ms.p50 | 16510.5 | 1220.7 |
| latency_ms.p95 | 142970.2 | 1522.0 |
| cost_per_1k_calls | 2.759 | 0.216 |

Evidence: same cases, labels and coverage

Gate (labels basis): **NOT PROMOTABLE**
- pass: relation_agreement — 73.5% vs 42.0% (max drop 3%, basis labels)
- pass: direction_agreement — 25.0% vs 10.0% (max drop 5%)
- FAIL: rate:partial — 1.0% vs 0.7%
- pass: rate:empty — 0.0% vs 0.0%
- FAIL: rate:parse_error — 1.0% vs 0.0%
- FAIL: rate:error — 7.2% vs 0.0%
- pass: cost_per_1k_calls — $0.2158 vs $2.7589 per 1k calls (92% cheaper; need ≥30% cheaper, or no dearer with higher agreement)
