# Implementation Checklist

Normative references:

- `unified-write-contract.md`
- `final-architecture-guardrails.md`

---

## ✅ Completed Phases

- **Phase 0** — Canonical Contracts and Guardrails
- **Phase 1** — Observability Foundation (OTEL)
- **Phase 2a** — Source URL Dedup + Provenance Surface
- **Phase 2b** — Internal Event Bus
- **Phase 3** — Digest Provenance v2 (`derived_from_ids`)
- **Phase 4** — Research Cache and Freshness
- **Phase 6** — Reconcile/Repair Tooling (internal + CLI/admin only; no MCP tool)
- **Phase 6.5** — SSE Event Delivery

---

## ⏸️ Deferred

**Phase 5 — Bulk Write v3**
Deferred until usage data demonstrates a need for batch writes. Single-write throughput is sufficient for current workloads. Plan: `bulk-write-v3.md`.

**Webhooks / Guaranteed Delivery**
No phase assignment. Revisit if a concrete consumer appears. Plans: `event-webhooks-plan.md`, `event-guaranteed-delivery-plan.md`.

---

## 🔵 Active

### Phase 7 — LCMA Rollout

See **`lcma-checklist.md`** for full MVP 1/2/3 breakdown.

Dependencies: Phases 0 through 6.5 complete ✅

---

## 🔲 Pending

### Phase 8 — API Ergonomics Cleanup — ✅ Done (option 2, PR #193)

Resolved by documentation grouping rather than grouped request objects: the
`lithos_write` docstring and `SPECIFICATION.md` present the flat options in
named sections (`provenance`, `freshness`, `lcma`). See the "API Ergonomics
Follow-up" section of `unified-write-contract.md`. Grouped input objects and
their conformance tests were not built and are not planned.

---

### Phase 9 — CLI Extension (Deferred Integration)

- [ ] Implement `cli-admin-client-split.md` (supersedes the deleted `cli-extension-plan.md`, PR #218): `lithos admin …` / `lithos client …` groups, `--output json`, exit codes
- [ ] Prioritize JSON output and read/list first, then CRUD, then graph/coordination/polish

Dependencies: Phases 0 through 8 complete

Exit criteria:
- CLI surfaces are built on top of the stabilized core contracts

---

## 🔄 Cross-Phase Conformance (Continuous)

- [ ] Maintain one conformance suite across single write, batch write, dedup, provenance, freshness, migration/rebuild, reconcile, event emission/delivery, and OTEL instrumentation
- [ ] Block milestone completion if conformance suite regresses
