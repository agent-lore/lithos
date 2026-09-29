# Lithos state review — 2026-09-29

Input to the October 2026 roadmap. Reviewed: the running production and
staging deployments, the planning documents under `docs/`, the open GitHub
issues, the `lithos-core` / `lithos-ecosystem` task-graph projects, and the
cognitive-memory layer (LCMA) against the current literature on agent memory.

Method, as for the loom review the day before: every claim was cross-checked
against the code, `git log`, merged PRs and the live stores, not against task
state alone. Nothing in this document closes, cancels or retags anything; the
dispositions below are recommendations for Dave to apply.

Sections:

1. [What the deployments say](#1-what-the-deployments-say)
2. [How Lithos is actually used](#2-how-lithos-is-actually-used)
3. [Planning documents](#3-planning-documents)
4. [GitHub issues](#4-github-issues)
5. [Task-graph projects](#5-task-graph-projects)
6. [LCMA: state and direction](#6-lcma-state-and-direction)
7. [Next steps](#7-next-steps)

## 1. What the deployments say

Read-only audit of the `lithos` (production) and `lithos-staging` containers,
2026-09-29: retained docker logs (since the 0.5.0 deploy on 09-26), Loki for
the 14 days before that, Prometheus for 7 and 30 days, `docker inspect`/`stats`,
and the data directories.

### 1.1 Health

Both containers are healthy and quiet. Restart count 0, no OOM, CPU under 1%,
~3.4 GiB RSS each. Retained logs hold 12 warnings and 12 errors in prod, 8 and
9 in staging; every error is shutdown noise from the host reboot. In steady
state there are no tracebacks, no ChromaDB errors, no `database is locked`, no
watchdog/inotify errors, no enrich-worker failures (0 failed nodes across 118
drains), and no LLM budget exhaustion. All OpenRouter calls returned 200.

| Signature | Env | Count | Assessment |
|---|---|---|---|
| uvicorn / OTEL exporter errors at container stop | both | 6 + 12 | shutdown artefacts of the host reboot (below) |
| `SSE replay gap … signalling resync` | prod | 5 | Lens reconnecting across the 0.5.0 deploy; the new `resync` event working as designed |
| `LLM call failed (ReadTimeout); retrying once` | prod 3, stg 1 | 4 | OpenRouter stalls at the 120 s timeout; 1 node lost after retry, ~5 min of drain each |
| `LLM adjudication … violated the response contract (N valid judgements)` | prod 1, stg 3 | 4 | partial judgements; degrades gracefully |
| `Version conflict … expected_version=1 actual_version=2` | prod | 1 | CAS working as designed |
| `Skipping invalid file …slice-5-test-gammer-project-context.md: Is a directory` | stg | 1/boot | test debris from May in the staging vault |
| FastMCP `Error validating tool 'lithos_task_list'` (`project=`, `limit=` unexpected) | prod | 1 at 08:32Z (+1 from this review session) | callers guess `project`/`limit` on `task_list` because the sibling `task_ready`/`task_blocked` accept them; fails before the tool wrapper, so it is invisible to metrics |
| ChromaDB posthog telemetry backoff / SSL retry | both | 33 | noise; `ANONYMIZED_TELEMETRY=False` silences it |

**The 23-hour outage** 2026-09-26 20:45Z → 09-27 19:53Z was the host being
down (kernel 7.0.0-30 → 7.0.0-34), not a Lithos failure. It is the largest
availability event in the window and explains every shutdown error.

### 1.2 Issues to be aware of

1. **HIGH, operational: `/home` is 100% full — 33 GB free of 3.5 TB.** Every
   Lithos store lives there (SQLite + WAL, `chroma.sqlite3` at 742 MB prod /
   903 MB staging, both vaults, and unbounded `json-file` docker logs for prod
   and staging). Writes will start failing with `disk I/O error` when it hits
   zero. Lithos itself is only ~14 GB of the volume (mostly `archive/` dirs);
   the bulk is elsewhere on `/home`. Docker's 650 GB of reclaimable images and
   build cache is on `/`, so pruning it will not help this volume. Set
   `max-size` on the prod/staging log drivers while at it (Lens already has
   10m×5).
2. **MEDIUM: Lens polling is 60–83% of all prod tool calls.** A flat 696
   `task_children` calls per hour since 09-28 20:00Z: an open tab on the epic
   view with the 120 s auto-refresh, fanning `task_children(recursive=True,
   include_closed=True)` over every open epic per render. Cheap per call
   (35 ms) and the server does not care, but it swamps every tool-usage
   metric and scales with epics × open tabs. See §7 for the API-side fix.
3. **LOW: observability gaps.** `otel_lithos_tool_errors_total` has no series
   at all (the counter has never incremented; an error-rate alert would show
   nothing); the LLM call-duration histogram's top bucket is 10 s while the
   mean is 16.5 s, so p95 is meaningless; salience gauges emit once a day
   (use `last_over_time[2d]`); Loki refuses multi-day log-line queries with
   `too many outstanding requests`; `increase()` misses the first increment
   of lazily created counters, so LLM `error`/`partial` show 0.
4. **LOW: housekeeping.** Staging carries a 207 MB `.chroma.corrupt-20260512…`
   leftover and the `*.md` directory above; prod reports 1 stale document;
   staging's image is an untagged dangling build.

### 1.3 Steady-state numbers (7 days, production)

| Metric | Value |
|---|---|
| Tool calls | 172,352 (staging 9,808) |
| Documents (Tantivy) / Chroma chunks | 5,582 / 97,971 |
| Search ops | semantic 1,404, fulltext 228, hybrid 22; avg 61 ms |
| Enrich queue depth now / max, lag | 0 / 24, ~213 s |
| LLM calls | 256 ok, 1 partial, 1 error; 615k tokens; mean 16.5 s |
| Inferred edges written / skipped (op-log, no candidates, call cap) | 924 / 200, 20, 13 |
| Salience mean / fraction below floor | 0.317 / 0 (no re-collapse since #402) |
| Active agents / claims | 64 / 6 |


## 2. How Lithos is actually used

Thirty days of production telemetry (Prometheus `otel_lithos_tool_calls_total`,
2026-08-30 → 2026-09-29) plus the read-access log in `coordination.db`.

### 2.1 Tool calls, 30 days, production

| Tool | Calls | Note |
|---|---:|---|
| `lithos_task_children` | 533,406 | 87% from Lens (465,774): the epic strip fans `task_children(recursive=True)` out over every open epic on each refresh |
| `lithos_task_list` | 80,408 | 57% Lens |
| `lithos_task_get` | 43,153 | |
| `lithos_cache_lookup` | 33,674 | influx dedup check before write |
| `lithos_write` | 31,660 | influx: 996 `created`, 29,804 `duplicate` (97% of submissions are re-submissions of known URLs) |
| `lithos_task_ready` / `task_blocked` | 18,247 / 18,069 | Lens + loom polling |
| `lithos_stats`, `lithos_agent_list` | 17,270 / 17,248 | Lens health polling (one call per ~3 min each) |
| `lithos_read` | 9,051 | 2,245 Lens; ~6,400 unattributed (`agent_id` not passed) |
| `lithos_agent_register` | 9,032 | one per session; fine |
| `lithos_task_edge_list` | 7,333 | |
| `lithos_list` | 3,847 | |
| `lithos_task_renew` | 3,316 | |
| `lithos_finding_post` | 1,580 | |
| `lithos_task_update` | 1,145 | |
| **`lithos_retrieve`** | **1,117** | ~37/day — the LCMA retrieval path |
| `lithos_task_create` / `task_complete` | 916 / 850 | |
| `lithos_finding_list`, `task_status` | 720 / 708 | |
| `lithos_related` | 523 | |
| `lithos_task_edge_upsert`, `task_cancel` | 239 / 224 | |
| **`lithos_search`** | **177** | ~6/day |
| `lithos_task_claim` / `release` / `spawn` | 168 / 52 / 30 | |
| `lithos_note_update` | 20 | |
| `lithos_task_reopen`, `lithos_tags` | 11 / 1 | |
| `lithos_edge_upsert`, `lithos_edge_list`, `lithos_conflict_resolve`, `lithos_node_stats`, `lithos_delete`, `lithos_agent_info`, `lithos_agent_archive` | 0 | no caller in 30 days |

Search engine operations: semantic 6,547, fulltext 1,130, hybrid 171 —
semantic is driven by `cache_lookup` and the retrieve scouts, not by humans.

### 2.2 What that means

- **Lithos in production is a task-coordination backbone first.** 94% of all
  tool calls are task-graph reads, and one consumer (Lens) accounts for 83% of
  all traffic through a polling pattern the API makes unavoidable: there is no
  bulk "subtree for these N epics" call and no way to ask "did anything change
  since X" cheaper than re-listing. The SSE `/events` endpoint exists and is
  not used by Lens for this.
- **The knowledge side is write-dominated by influx and read by almost no one.**
  The corpus is 5,582 notes (up from 3,115 in July); influx authored 4,228 of
  them (`articles/` 3,385, `papers/` 843). In 30 days, 138 of 3,007 article
  notes were touched at all and 314 have *ever* been read or returned by a
  search. `research/` (383 notes) is returned by searches (813 hits) but was
  read zero times. The notes that are actually read and re-read are Dave's own:
  `user/daily` (63 of 87 touched in 30 days) and `user/planning` (65 of 83),
  via the planning-coach agent `garuda`, plus two project-context notes that
  loom loads on every run (5,150 reads between them, unattributed).
- **Agents that retrieve knowledge are a short list:** `garuda` (planning
  coach; 300 search results, 324 reads), `ai-agents-briefing` (170 results),
  `hema-events-scout`, `codex-agent-foundations` (57 reads), Lens (1,961 reads
  via the note pages). Coding agents (`claude-code-*`, `codex-*`) read
  Lithos a handful of times a month.
- **The LCMA tools built for agents to *assert* knowledge structure are
  unused** (`edge_upsert`, `conflict_resolve`, `node_stats`: zero calls). The
  typed-edge graph is being filled only by the background LLM inference
  (1,178 successful LLM calls in 30 days, 11 errors). Retrieval receipts
  exist but no caller passes `task_id`, so the feedback loop designed in
  MVP-2 (`cited_nodes` / `misleading_nodes` on `task_complete`) has no data.
- **Human usage is indirect.** Dave reaches Lithos through Lens (tasks), the
  planning-coach agent (his own notes), and Claude Code sessions. There is no
  surface through which he browses or curates the *knowledge* half; Lens K2/K3
  (knowledge graph + cognitive search) are still "planned" in the Lens roadmap.

The honest framing for October: **the coordination product is real and
load-bearing; the knowledge product is an unread inbox with a good retrieval
engine nobody is pointed at.** Section 6 is about changing that.

*Dave's reading (2026-09-29):* the influx notes are unread because he has been
waiting on Lens to have a usable knowledge browser before navigating them, not
because the intake is unwanted. That makes the read-shape work (K1, the input
to Lens K2) the item that unlocks the corpus for its owner, and it is why the
phase-4 direction was endorsed the same day.

## 3. Planning documents

Audit of `docs/plans/*.md`, `docs/plans/deferred/`, `SPECIFICATION.md` §11,
`mcp-roadmap-alignment.md`, `CONTEXT.md` and the ADR follow-ups, each item
checked against `src/lithos` and merged PRs.

### 3.1 Dispositions applied in this PR

| Document | Finding | Action taken |
|---|---|---|
| `plans/seam-tightening-search.md` | Phases 1–7 delivered; the "out of scope" graph/provenance folds and `reconcile.py` deletion also landed (#389) | moved to `plans/archive/` with a banner |
| `plans/task-graph-coordination-extension.md` | Phases 1–3 delivered (#342, #343, #356, #357); header still said "Proposal" | moved to `plans/archive/`; Phase 4 stub carried to `future-improvements.md` |
| `plans/implementation-checklist.md` | Phase 8 was resolved by documentation grouping (#193) not grouped request objects; Phase 9 pointed at `cli-extension-plan.md`, deleted in #218 | Phase 8 marked done with the decision; Phase 9 points at `cli-admin-client-split.md` |
| `plans/unified-write-contract.md` | listed error codes `invalid_uuid`, `unsupported_feature`, `path_collision`, `stale_write_conflict` that were never implemented; missed `ambiguous_id_prefix`, `note_not_found`, `content_too_large` | core-code list now mirrors `SPECIFICATION.md` §10.2 |
| `SPECIFICATION.md` §11 | still listed contradiction resolution, quality scoring and multi-hop links as future although `lithos_conflict_resolve`, `lithos_node_stats` and `graph_depth` ship; §5.6 labelled "MVP 1" while listing MVP-2 tools | struck with pointers; retitled |
| `plans/future-improvements.md` | 4 of 7 items shipped (conflict resolution, quality signals, namespace fields, task graph); cited the deleted CLI plan | rewritten — see the file |
| `plans/lcma-checklist.md` | MVP 1/2 and WS1/WS2-slice-1 shipped but never ticked; every remaining MVP-3 item is carried by the phase-4 plan | ticked, then moved to `plans/archive/` with a WS → K mapping banner (Dave's call, 2026-09-29) |

### 3.2 Dispositions recommended, not applied

| Document | Status | Recommendation |
|---|---|---|
| `plans/cli-admin-client-split.md` | not started; the only CLI direction on record | keep; refresh its command table (`recalibrate-salience`, `extract-entities` exist; `inspect`/`audit` are undocumented in `docs/cli.md`) when the work is scheduled |
| `plans/final-architecture-guardrails.md` | in force; still mentions `batch.db`, a pre-projection phasing rule and webhook/batch conformance rows for plans that are deferred | light edit when next touched |
| `plans/target-search-schema.md` | live registry, slightly behind code: `entities` field (extractor v4) unregistered, `created_at` listed but not indexed | small fix when the schema next changes |
| `plans/lcma-design.md` | see §6 | §5.5 annotated with the shipped rerank formula in this PR |
| `plans/deferred/bulk-write-v3.md`, `event-webhooks-plan.md`, `event-guaranteed-delivery-plan.md` | no consumer has asked in 5–7 months; all cite `server.py` closures that no longer exist | keep parked; the influx duplicate rate (§2.1) is the first real argument for a cheap `has_source_url` / bulk-exists check, not for bulk write |
| `docs/mcp-roadmap-alignment.md` | last updated 2026-03-12; names tools that never existed (`lithos_task_abandon`, `lithos_findings`); omits the task graph, short ids, `/audit`, SSE `resync` | rewrite as a short positioning page or delete; nothing links to it |
| `docs/cli.md` | omits `inspect` and `audit` | one-hour doc fix |
| ADR-0006 slices 2–3 | consolidation edges and reinforcement still write to `edge_store` directly rather than through intake (`lcma/enrich.py`, `lcma/edge_reinforce.py`); `lithos_conflict_resolve` dual-writes edges + notes without the promised atomicity ADR | open one tracking issue; not urgent (single-process writer) |
| `plans/archive/otel-plan.md` Phase 3 | LLM tracing was deferred "until an LLM client exists" — `lcma/llm.py` now exists and emits counters but no spans with `gen_ai.*` attributes | fold into the LCMA operability work in §6 |

## 4. GitHub issues

16 open issues, 0 open PRs, CI green for the last 100 runs (one non-code
`Review Agent Skills` failure on 09-16). Each issue was read with its comments
and checked against `src/lithos` and merged PRs.

### 4.1 Already done → close

| Issue | Evidence |
|---|---|
| #420 collision-proof task edit stamps | shipped inside PR #419 (0.5.0 changelog: `max(now, prior + 1µs)` inside the write transaction, stamp echoed in responses/events, startup migration) |
| #378 skip publishing unchanged agent-skill plugins | PR #414 (2026-08-28): "Determine changed plugins" step; last four publish runs green |
| #200 `server.py` concentration (3,370 lines, 28 closures) | `server.py` is 899 lines with zero tool closures; 38 tools live in `src/lithos/tools/*.py` (PRs #370–#375) |
| #308 CI flake + hang | HF model baked into the image with retry (#330), `HF_HUB_OFFLINE=1` (#319), uv cache (#331); 25/25 CI passes since 09-05, no hung job since June. Optional S follow-up: `timeout-minutes` on the Test/Integration jobs |
| #136 Phase 9 CLI extension | superseded by `plans/cli-admin-client-split.md` (PR #218 deleted the plan it points at). Open a fresh issue only when the split is scheduled |
| #285 SSE endpoint-level e2e tests | `tests/test_event_delivery.py` covers type/tag filters, `since`, `Last-Event-ID` and replay+filter through the handler; the remaining delta is transport plumbing with high flake risk |

### 4.2 Still valid, worth doing

| Issue | Priority / size | Note |
|---|---|---|
| #421 edge inference never retries after an LLM outage | H / M | `LlmError` path leaves no op-log row and nothing re-enqueues; a contract-violation with 0 valid judgements is recorded as done; 140 prod nodes stuck from the 09-08/09-13 outages, 2,572 more never adjudicated because slice-2 backfill does not exist. One fix covers both — see §6 |
| #367 self-heal when ChromaDB is unrecoverable | M / S | `server.py` logs "remains unavailable after repair attempt" and continues; compose has no restart-on-unhealthy. Exit non-zero after N failed repairs (with a crash-loop guard) is the S-sized answer |
| #222 dedup by arxiv-id / source-stable identifier | M / M | dedup is still `normalize_url(source_url)` then slug; influx is 76% of the corpus, so this is where duplicates come from |
| #292 task event payloads carry tags + metadata | M / M | `updated_at` in payloads (#415/#419) already gives consumers a cheap change signal; remaining ask is `event.tags` + `metadata` + `changed`. Sequence before #293 |
| #293 tags + metadata on findings | M / M | findings table unchanged; line refs in the issue are stale (tools moved) |
| #201 `list_all` reads documents sequentially | L / S | bounded by `limit` since filtering went index-based (#306/#316); one `asyncio.gather` |

### 4.3 Needs a decision (recommendation in bold)

| Issue | Options |
|---|---|
| #207 no scope enforcement on `search`/`read`/`list` | **Docstring caveat on the three tools and close.** Multi-agent isolation is not a real requirement in this deployment; `access_scope` is advisory by design |
| #129 `as_of` filter on `lithos_search` | **Close wontfix as filed.** No consumer in six months; `updated_at <= X` is a weak proxy without history. The real need (temporal validity of *claims*) is addressed differently in §6 |
| #49 document versioning / history | **Document "put the vault under git" as the supported history and close.** CAS (`expected_version`) already removes the silent-overwrite premise. Reopen as a PRD only if an audit use case appears |
| #333 path-style wiki-link targets as entities | **Close wontfix** — the issue records the 2026-06 "accept for now" decision; it is a parked decision, not work |

## 5. Task-graph projects

294 open tasks across 22 projects in the tracker; `lithos-core` has 30 and
`lithos-ecosystem` 3. Sixteen `lithos-core` tasks are loom mirrors of the
GitHub issues above (each carries `metadata.github_issue_number`; their
dispositions follow §4). The rest were verified against the code.

### 5.1 Done → close (with the outcome text, or the date is lost)

| Task | Outcome to record |
|---|---|
| `6dbc3b80` task_update compare-and-set | Shipped in PR #419 (0.5.0): `expected_updated_at` CAS with `version_conflict` envelope, `add_tags`/`remove_tags` set-ops, collision-proof stamps (#420). `append_description` spun out to `87866e06` |
| `69c75e57` `_parse_datetime` silently returns None | Fixed in PR #234 (2026-05-03), now `sqlite_datetime.parse_datetime` with a WARN log; GitHub #205 closed then, the task never was |
| `f80b6eb4` epic: architecture deepening review 2026-07 | 8 of 11 children completed (PRs #370–#400), 2 cancelled; closes once `971f8892` is cancelled |
| `cfd3f24f` (#200 mirror), `653c32b3` (#420 mirror), `ee1ff035` (#378), `4c8cea68` (#308), `761de2e8` (#136), `00ae6df6` (#285) | close with the GitHub issue |

### 5.2 Still valid

| Task | Priority / size | Note |
|---|---|---|
| `60b3e135` error envelopes on `lithos_finding_list` never reach clients | **H / S** | Reproduced today through a validating `fastmcp.Client`: the tool advertises `outputSchema` `dict[str, list[…]]`, so any error envelope fails output validation and the client sees `Output validation error: 'error' is not of type 'array'`. Same annotation on `lithos_task_list`, `task_children`, `task_status`, `agent_list` — the 0.5.0 `task_not_found` behaviour advertised in the changelog does not reach real clients on those five tools. The in-repo conformance test passes only because `tests/helpers.py` bypasses output validation. Fix is one line per tool plus a regression test through a validating client. **0.5.1 candidate** |
| `3dda1b1f` trace-context log filter on the root logger | H / S | still `root_logger.addFilter(...)` in `telemetry.py`; no lithos log line carries `otelTraceID`, so Loki→Tempo correlation is broken. The only test calls the filter directly |
| `41de9716` `_reset_for_testing` clears the wrong module's latch | L / S | bundle with the above |
| `bd66d57c` task edges cannot be deleted | M / S–M | no delete path; the `parent_exists` error still tells callers to remove an edge they cannot remove; Lens T3 §5C.2 depends on it |
| `6383a81b` `task_blocked` takes no `task_id` | M / S | Lens task-detail page still carries the "list store-wide and scan" workaround that returns a wrong empty answer |
| `87866e06` `append_description` | M / S | designed in the task; scaffolding from #419 in place |
| `e0e31654` `lithos_list` server-side ordering | L / S | cheap: `updated_at` is already in the corpus index cache |
| `97cd00bb` Chroma semantic coverage gap | re-scope to M / M | acute half done (backfill 2026-07-22, 30% → 100%); drift detection is still Tantivy-only and there is no scheduled reconcile, so a repeat would be invisible. Retitle "Chroma coverage drift alert + scheduled reconcile safeguard", drop [HIGH] |

### 5.3 Not worth doing (cancel, with reason)

| Task | Reason |
|---|---|
| `971f8892` split `StatsStore` into subdomains | the shared `AsyncSqliteStore` base landed (#398); the 5-way split is cosmetic and the module keeps absorbing phase-3 tables. Re-file if it blocks something |
| `49444309` make LCMA mandatory | four branch sites remain; the payoff is a handful of gates against forcing spaCy + sentence-transformers on every deployment. Record the product decision and cancel |
| `fc4b0669` explicit feedback loop unused | the re-plan chose implicit signals (#402). The *adoption* half (skill + influx passing `receipt_id`/`cited_nodes`) is carried into §6 as part of the retrieval fix; cancel this task in favour of that |
| `lithos-ecosystem`: `54a85e80` DeerFlow showcase, `ab47d1fa` ACP endpoint doc, `ed283984` Codex plugin packaging | filed 2026-03-28 by agent-zero, untouched for six months, no consumer, `docs/integrations/` never created. Positioning documents for ecosystems that have not asked. Cancel; re-file if a real integration request arrives |

## 6. LCMA: state and direction

Full code map, checklist reconciliation and substrate measurement are in the
review's working notes; the numbers below are from read-only copies of the
production `stats.db` / `edges.db` taken 2026-09-29 and reproduced against the
receipts table.

### 6.1 What shipped and what did not

| Layer | Status |
|---|---|
| MVP 1–2 (11 scouts, Terrace-1 rerank, receipts, working memory, enrich worker with decay / consolidation / NER / contradiction workflow, `conflict_resolve`, `node_stats`) | shipped 2026-04/05; the checklist boxes were never ticked — fixed in this PR |
| MVP 3 prerequisite: salience recalibration (`e7d8ef60`, PR #402) | shipped; no re-collapse (fraction below floor = 0 on both envs) |
| WS1 LLM backbone + WS2 slice 1 typed-edge inference (PRs #405/#406/#409/#410) | shipped, prod live since 2026-08-11: 5,907 inferred edges, mean confidence 0.80, 1,846 calls / 4.4M tokens in 47 days (38% of the daily budget on average, never capped) |
| WS2 slice 2 (sweep backfill + re-inference policy) | not started — 2,572 notes written before go-live have never been adjudicated; only 30% of notes have been a focus node |
| WS3 temperature/exploration, WS4 concept nodes, WS5 analogy scout, WS6 metamemory, WS7 read-shape for Lens | not started; `compute_temperature` is a stub (0.5 on all 4,306 receipts, terrace always 1) |
| `lcma-design.md` MVP-3 section | revised in PR #401 as decided; §5.5 still showed the never-shipped rerank formula (annotated in this PR with the shipped one) |

### 6.2 The graph is no longer empty; the ranking is now the problem

The July finding ("typed-edge graph semantically empty, 2 genuine relation
types, 17% coverage") is resolved: 8,494 edges, 62% of notes carry a typed
edge, and `supports` (2,563), `analogy_to` (1,462), `refines` (811),
`is_example_of` (683), `depends_on` (276) and `contradicts` (113) all exist
with rationales. `scout_graph` uses them (inferred weight 0.6–0.98 outranks
wiki-links at 0.5) and 89% of nodes surfaced in the last 30 days carry an
inferred edge.

What the substrate now shows instead is a **popularity feedback loop** that
makes `lithos_retrieve` return the same hub notes to everyone:

| Signal (30 days, 1,120 prod receipts, 96% from influx) | Value |
|---|---|
| Result slots taken by the top-10 notes | **40%** |
| Slots filled by a node found *only* by the coactivation scout | **32%** (was ~10% in June–July) |
| Most frequent scout set on a result | `scout_coactivation` alone (1,783 of 5,644 slots); `vector+lexical` is third (912) |
| Salience distribution | 90.3% of nodes sit exactly at the 0.30 floor; 21 nodes at 1.0 |
| Explicit feedback | `cited` 10, `misleading` 0, `ignored` 15 across 4,040 node rows |

Mechanism: influx retrieves with `limit=5` → coactivation increments for every
pair → the coactivation scout scores raw counts min-max normalised (the top hub
is always 1.0) → those nodes reach working-memory activation ≥2 → consolidation
adds +0.01 salience per task → salience 1.0 + usage score 1.0 add +0.2 in the
rerank, which beats a perfect semantic match (0.215 + 0.03). The recalibration
in #402 fixed the collapse but, combined with the new usage term, amplified the
rich-get-richer loop. The hubs are arbitrary influx articles with 400–1,250
retrievals. For influx, which is 96% of callers, `lithos_retrieve` is currently
worse than hybrid search on three or four of its five slots.

Two design facts compound it: the rerank *averages* scout weights instead of
summing, so multi-scout agreement is not rewarded; and `note_type_prior`
cannot vary when 78% of notes are `summary` and 21% `observation`.

### 6.3 Other debt worth naming

- #421 (no retry after an LLM outage) and the missing slice-2 backfill are one
  fix: a set-difference over `edge_inference_log` in `full_sweep`, budget-capped,
  with 0-valid-judgement runs treated as not done, plus a `lithos infer-edges
  --backfill/--purge` command. No test covers the sweep path (deferred in
  `test_edge_inference.py`).
- The op-log is keyed on `doc_version`, so any frontmatter touch (entity heal,
  salience write-back) re-spends ~2.4k tokens, while neighbourhood drift never
  triggers re-inference.
- 113 `contradicts` edges exist, 112 unreviewed, and none are surfaced because
  no caller passes `surface_conflicts=True`.
- `validate_task_feedback` silently drops feedback when no receipt matches; no
  caller (skill or influx) passes `receipt_id`/`cited_nodes`, so the explicit
  loop has never had a chance.
- `lithos_related` still returns raw `edges.db` rows (ids, no titles, no
  `source` mapping, no ranking): the 5,907 inferred edges are invisible to a
  human and awkward for an agent. Lens K2 ("visualise what influx has been
  accreting") is blocked on exactly this.
- The wiki-link graph is 123 edges for 5,582 notes: the Obsidian-style
  connectivity the project is named for barely exists in the corpus.
- Cost control is a soft daily cap; no alert on `outcome=error` bursts (the
  09-03/06 outage was found by reading logs).

### 6.4 Direction: from a retrieval engine to a connected knowledge base

The literature pass ([`2026-09-agent-memory-literature.md`](2026-09-agent-memory-literature.md))
converges on a few things that Lithos is unusually well placed to do, because
the corpus is already Markdown on disk with provenance:

- **Compile, don't only retrieve.** The systems that beat atomic-fact and
  triple stores in 2026 (ProGraph, TriMem, Infini Memory, Karpathy's LLM wiki)
  maintain *prose pages* per entity/topic, rewritten as facts change. For the
  owner that is "what do I know about X" as a note, not a search.
- **Knowledge is superseded, memory decays.** Applying Ebbinghaus decay to
  facts is a category error (Missing Knowledge Layer, MemStrata); the July
  salience collapse was that error in practice. Bi-temporal validity with
  deterministic supersession is what every serious 2026 system does for
  evolving facts.
- **Close the loop with receipts, not labels.** RMM/REALM show a reranker
  trained on what was actually cited beats static weights; Lithos already
  writes the receipts.
- **Lint and digest are the owner's surface.** Every LLM-wiki implementation
  that people keep using has a lint pass (contradictions, orphans, missing
  pages) and a digest; Readwise's recall half-life is the one resurfacing
  mechanism with staying power.
- **Selective priming beats always-on context** for agents (Remember When It
  Matters, Claude Code's 200-line memory budget).
- **Not worth copying:** GraphRAG community summaries, triple-first storage,
  RL-trained memory managers, decay-on-everything, vendor leaderboard scores.

The direction, its workstreams and an October slice are written up as
[`docs/plans/lcma-connected-knowledge.md`](../plans/lcma-connected-knowledge.md).

## 7. Next steps

### 7.1 Already done: close or cancel

- GitHub: close #420, #378, #200, #308 (fixed); #136, #285 (superseded).
- Tracker: complete `6dbc3b80`, `69c75e57` with the outcomes in §5.1; close the
  epic `f80b6eb4` after cancelling `971f8892`; close the six loom mirrors with
  their issues.
- Docs (done in this PR): `seam-tightening-search.md` and
  `task-graph-coordination-extension.md` archived; checklist, write contract,
  SPECIFICATION §11 and LCMA checklist corrected; `future-improvements.md`
  rewritten.

### 7.2 Open work still worth doing

Ordered by value to the owner; sizes are S (<1 day), M (days), L (a week+).

| # | Work | Size | Why now |
|---|---|---|---|
| 1 | Free space on `/home` and cap docker log sizes | ops | every store is on a 100%-full volume |
| 2 | `60b3e135` widen the output schema on the five `dict[str, list[…]]` tools + regression test through a validating client | S | real clients cannot see error envelopes on `finding_list`, `task_list`, `task_children`, `task_status`, `agent_list`; 0.5.1 |
| 3 | **K0 retrieval fix**: break the coactivation/salience/usage loop; surface `contradicts` by default; make the skill and influx pass `receipt_id`/`cited_nodes` | M | `lithos_retrieve` is currently worse than `lithos_search` for its main caller; measurable (top-10 share 40% → <15%) |
| 4 | #421 + WS2 slice-2 backfill in `full_sweep`, `lithos infer-edges` CLI | M | 70% of notes never adjudicated; spend is 38% of budget |
| 5 | **WS7 read-shape** on `lithos_related` / `lithos_edge_list` (titles, `source`, ranked `neighbours`, filters) | M | the only way 5.9k inferred edges become useful to a human or an agent; unblocks Lens K2 |
| 6 | `3dda1b1f` + `41de9716` telemetry fixes; `tool_errors` counter actually incrementing; LLM histogram buckets | S | Loki→Tempo correlation and error-rate alerts are silently broken |
| 7 | `bd66d57c` task-edge delete, `6383a81b` `task_blocked(task_id)`, `87866e06` `append_description` | S each | Lens T3 (loom's October customer) needs the first two |
| 8 | #367 exit-on-unrecoverable-Chroma; `97cd00bb` re-scoped as Chroma coverage alert + scheduled reconcile | S + M | the two halves of "a degraded server should not look healthy" |
| 9 | #222 source-stable identifier dedup; #292 then #293 event/finding payloads | M each | influx is 76% of the corpus; loom wants routing on tags |
| 10 | Digest-lite (K4): a daily note listing what agents changed, superseded claims, open contradictions, and 3–5 resurfaced notes | S–M | the first owner-facing knowledge surface that needs no UI; `garuda` already reads `user/planning` daily |

### 7.3 Open work no longer worth doing

- GitHub #129 (`as_of` on search), #333 (path-style entities): close wontfix.
  #207: docstring caveat and close. #49: document "vault under git" and close.
- Tracker `971f8892`, `49444309`, `fc4b0669` (superseded by 7.2 #3), and the
  three `lithos-ecosystem` positioning-doc tasks: cancel with the reasons in
  §5.3.
- `future-improvements.md` §6 AgentRace benchmark: dropped (no harness, no
  external consumer). The deferred bulk-write/webhook plans stay parked.
- `docs/mcp-roadmap-alignment.md`: delete or rewrite; it is six months stale
  and names tools that never existed.
- Architecture work to park for October, per the usage evidence: the
  `admin`/`client` CLI split, the `StatsStore` subdomain split, LCMA
  optionality, ADR-0006 slices 2–3 (single-process writer, no incident).

### 7.4 Gaps to track (new tasks or PRD)

| Gap | Evidence | Suggested tracking |
|---|---|---|
| Retrieval popularity loop | §6.2 | task, HIGH, part of K0 in the new plan |
| LCMA direction: supersession/validity, compiled concept & entity pages, lint + digest, gated priming, receipt-trained rerank, evaluation harness | §6.4, literature brief | `docs/plans/lcma-connected-knowledge.md` (this PR) → epic + tasks once Dave has reacted |
| No bulk subtree read; Lens polls `task_children` per epic every 120 s | §1.2, §2.1 | task: `lithos_task_children(task_ids=[...])` or an `epic_rollup` read; Lens side: refresh from SSE `task.*` events instead of the timer |
| `lithos_task_list` lacks `project` / `limit` while `task_ready` / `task_blocked` have them | §1.1 (two callers guessed them today) | task, S: add both for consistency (`limit` already exists on the siblings) |
| 97% of influx writes are duplicate re-submissions | §2.1 | task: a batch `lithos_cache_lookup(source_urls=[...])` existence check, or influx-side change; the first real argument for anything from `deferred/bulk-write-v3.md` |
| 70% of `lithos_read` calls are unattributed | §2.1 | task, S: make `agent_id` required on read/search/list, or derive it from the session's registered agent |
| `tool_errors_total` never increments; FastMCP validation errors bypass the JSON logger | §1.2 | task, S |
| No `gen_ai.*` spans on LLM calls (otel-plan Phase 3) | §3.2 | fold into K0/K7 |
| `docs/cli.md` omits `inspect`/`audit`; `mcp-roadmap-alignment.md` stale | §3.2 | doc tasks, S |
| Staging vault debris (`*.md` directory, `.chroma.corrupt-*`) | §1.2 | ops, minutes |

### 7.5 A realistic October slice for lithos-core

Dave's attention is the binding constraint (the September review said so and
Toggl agreed), loom's October customer is Lens T3, and lithos-core's real
consumers are Lens, loom and influx. The slice that serves them and the owner
without opening an architecture front:

1. **Unblock consumers (week 1):** 7.2 #1, #2, #7 — ops, the 0.5.1 output-schema
   fix, task-edge delete and `task_blocked(task_id)` for Lens T3.
2. **Make retrieval honest (weeks 2–3):** K0 — the loop fix with its two
   measurements written down, #421 + backfill, feedback plumbing in the skill
   and influx.
3. **Make the graph visible (week 3–4):** WS7 read-shape, so Lens K2 can be
   the November customer and agents get explainable neighbourhoods.
4. **One owner-facing artefact (week 4):** digest-lite as a daily note.

Everything in `lcma-connected-knowledge.md` beyond K0/K1/K4-lite is Q4
material and should be pulled forward only by friction from real use, as the
August rule says.

