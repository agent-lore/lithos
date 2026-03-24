# Lithos ↔ MCP 2026 Roadmap Alignment

This document maps Lithos features to items on the MCP 2026 roadmap. It is a positioning reference — useful for understanding where Lithos is ahead of, aligned with, or complementary to the evolving MCP standard.

Last updated: 2026-03-12
MCP roadmap source: https://modelcontextprotocol.io/development/roadmap

---

## Summary

| MCP Roadmap Item | Lithos Status | Notes |
|-----------------|---------------|-------|
| Agent discovery / Server Cards | ✅ Implemented | `lithos_agent_register`, `lithos_agent_list`, `lithos_agent_info` provide a shared agent directory with type metadata |
| Provenance metadata / Audit trails | ✅ Implemented | `derived_from_ids`, `source_url`, `lithos_provenance` — BFS traversal of provenance graph |
| Agent task lifecycle (retry, expiry) | ✅ Implemented | `lithos_task_claim` with TTL, `lithos_task_complete`, `lithos_task_status`; expiry via `expires_at` |
| Triggers and event-driven updates | ✅ Implemented (ahead) | SSE event delivery (Phase 6.5) — real-time knowledge change stream |
| Scalable session handling | 🟡 Partial | Stateless MCP tool surface; no session resumption semantics defined |
| Enterprise observability | ✅ Implemented | OpenTelemetry instrumentation throughout (Phase 1) |
| Security & authorization scoping | 🟡 Partial | `access_scope` field (LCMA MVP 1); no OAuth/SSO integration |
| Conformance test suites | 🔵 In progress | Cross-phase conformance suite (continuous, tracked in `implementation-checklist.md`) |
| Streaming / reference-based results | 🔲 Not started | Not planned; no large binary payloads in current use cases |

---

## Detailed Alignment

### Transport Evolution and Scalability

**MCP roadmap (Priority 1):** Evolve Streamable HTTP to run statelessly across multiple server instances, define scalable session handling, and introduce MCP Server Cards — structured server metadata discoverable via `.well-known` URL.

**Lithos:** Lithos operates as a stateless MCP tool server; each tool call is independent with no session affinity requirement. The agent registry (`lithos_agent_register`, `lithos_agent_list`) is broadly analogous to the Server Cards concept — it provides a discoverable directory of active agents with type and activity metadata, queryable by any connected client.

**Gap / opportunity:** Lithos does not implement `.well-known` Server Card discovery. If the Server Card WG produces a standard format, Lithos could expose its tool surface and capability metadata at that endpoint. The agent registry is richer than what Server Cards are scoped to (servers, not agents), but a capability map could be generated from the existing registry data.

---

### Agent Communication (Tasks Primitive)

**MCP roadmap (Priority 2):** Close lifecycle gaps in the Tasks primitive — specifically retry semantics (what happens on transient failure) and expiry policies (how long results are retained after completion, how clients learn a result has expired).

**Lithos:** The task coordination layer (`lithos_task_claim`, `lithos_task_complete`, `lithos_task_abandon`, `lithos_task_status`) implements claim-based work distribution with TTL-based expiry. Tasks expire automatically if not completed within the TTL window, releasing them back to the pool — directly addressing the "expiry policies" gap the roadmap identifies. The `lithos_findings` mechanism provides a result retention model beyond simple completion.

**Gap / opportunity:** Lithos does not expose retry semantics — a failed task is abandoned and re-claimable, but there is no built-in retry count or exponential backoff policy. If the Agents WG defines a standard retry contract, Lithos could surface `retry_count` and `last_error` in task state.

---

### Governance Maturation

**MCP roadmap (Priority 3):** Formalize contributor ladder, delegation model, and WG charters.

**Lithos:** No direct feature alignment. Governance maturation affects the MCP specification process; Lithos is a consumer and implementor. As MCP governance matures, Lithos will benefit from faster SEP review for features it depends on (e.g., event-driven updates, agent discovery).

---

### Enterprise Readiness

**MCP roadmap (Priority 4):** Audit trails and observability, enterprise-managed auth, gateway and proxy patterns, configuration portability.

**Lithos:**

- **Audit trails:** OpenTelemetry instrumentation (Phase 1) provides structured traces and spans across all tool calls. The LCMA `receipts.jsonl` audit log (MVP 1) adds retrieval-specific audit history.
- **Observability:** OTEL spans are emitted for writes, reads, searches, and coordination operations. These feed into standard observability pipelines.
- **Enterprise-managed auth:** Not implemented. Lithos inherits auth from the MCP transport layer; no SSO or OAuth integration exists.
- **Configuration portability:** `LithosConfig` is file-based and portable across deployments. No client-specific configuration surface yet.

**Gap / opportunity:** As the Enterprise WG produces auth guidance (SSO, cross-app access), Lithos should adopt it at the transport boundary rather than building a parallel solution. The `access_scope` field (LCMA MVP 1) provides a scoping primitive that may align with fine-grained authorization scopes.

---

### Triggers and Event-Driven Updates

**MCP roadmap (On the Horizon):** A standardized callback mechanism (webhooks or similar) for servers to proactively notify clients when new data is available, with defined ordering guarantees.

**Lithos:** SSE event delivery (Phase 6.5) is already implemented — Lithos emits real-time events over Server-Sent Events when knowledge is written, updated, or indexed. This is ahead of the MCP roadmap's "on the horizon" timeline. When MCP standardizes a callback/webhook mechanism, Lithos's SSE delivery can be mapped or extended to conform.

**Gap / opportunity:** Lithos's SSE delivery is not yet a standard MCP extension. If MCP produces a triggers/webhooks SEP, Lithos should align its event schema and delivery guarantees with the standard rather than maintaining a parallel interface.

---

### Result Type Improvements (Streaming / References)

**MCP roadmap (On the Horizon):** Streamed results for incremental output; reference-based results for large payloads.

**Lithos:** Not planned. Lithos knowledge notes are small markdown documents; large payload streaming is not a current use case. If LCMA retrieval results grow in volume, reference-based result returns could become relevant in future.

---

### Security and Authorization

**MCP roadmap (On the Horizon):** Finer-grained least-privilege scopes, OAuth mix-up attack guidance, secure credential management, vulnerability disclosure program.

**Lithos:** The `access_scope` field (LCMA MVP 1) provides advisory visibility scoping (`shared|task|agent_private`) and is explicitly not a security boundary — all agents operate in the same trust domain per `SPECIFICATION.md` section 1.2. Project-level scoping is handled by `namespace`, not `access_scope`. When MCP defines fine-grained scopes, Lithos's `access_scope` + `namespace` model will need review to determine whether it should be promoted to an enforced boundary or remain a retrieval hint.

---

### Conformance Test Suites

**MCP roadmap (Validation):** Automated conformance verification for clients, servers, and SDKs.

**Lithos:** The cross-phase conformance suite (`implementation-checklist.md`) covers single write, batch write, dedup, provenance, freshness, migration/rebuild, reconcile, event emission/delivery, and OTEL instrumentation. Milestone completion is blocked on conformance suite regression. This aligns with the MCP roadmap's conformance investment and positions Lithos to adopt SDK tier requirements as they are formalized.

---

## Features Ahead of the Roadmap

Lithos features that are not yet on the MCP roadmap but address problems the roadmap is likely to reach:

- **Multi-agent task coordination** — claim-based work distribution with TTL, findings, and task lifecycle. The MCP Tasks primitive (SEP-1686) is the closest analogue but lacks Lithos's multi-agent coordination semantics (claiming, abandonment, findings accumulation).
- **Knowledge freshness / TTL** — `expires_at`, `is_stale`, `lithos_cache_lookup`. Content-level freshness is not addressed in the MCP roadmap; it is a Lithos-specific primitive for research and retrieval workloads.
- **SSE event delivery** — real-time event stream for agents to subscribe to knowledge changes (Phase 6.5). Ahead of the MCP "Triggers and Event-Driven Updates" item which is still "on the horizon."
- **LCMA retrieval** (Phase 7) — multi-scout cognitive retrieval with reinforcement learning, namespace isolation, and provenance-aware re-ranking. Goes significantly beyond simple RAG and is not addressed in the MCP roadmap.

---

## Governance and Compatibility Notes

- Lithos MCP tool surface may change pre-1.0; on-disk markdown format has compatibility guarantees (see `SPECIFICATION.md`)
- All Lithos tools follow the MCP tool contract; no proprietary extensions to the protocol itself
- Lithos will adopt MCP standard extensions (Server Cards, Tasks lifecycle, conformance tiers) as they are finalized by relevant Working Groups
