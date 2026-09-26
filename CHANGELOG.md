# Changelog

## [0.5.0] — 2026-09-26

Seven weeks of work on `main` since 0.4.0: the LCMA phase-3 groundwork
(salience recalibration, an LLM-synthesis substrate and typed-edge
inference), agent-roster hygiene, optimistic concurrency on tasks,
short id prefixes everywhere, and the 2026-07 architecture-cleanup
roadmap. The tool surface grows from 38 to 39 (`lithos_agent_archive`).

### Behaviour changes — read before upgrading

Permitted by the pre-1.0 compatibility policy in `SPECIFICATION.md §1.4`.
Each is detailed in its own entry below.

- **Error codes tightened for unknown ids.** A 6–35-char task id that
  matches nothing now returns `task_not_found` on every task tool —
  including claim/renew/release, which returned `claim_failed` /
  `claim_not_found`, and status/edge_list/children/finding_list, which
  returned silently empty results. An id shorter than 6 chars that
  matches nothing exactly is `invalid_input`. `lithos_write` with an
  unknown `id` returns a `note_not_found` envelope instead of a
  protocol-level `ToolError`.
- **SSE consumers must handle `event: resync`.** `GET /events` now emits
  a `resync` control event when a reconnecting client's `Last-Event-ID`
  is not in the replay buffer (evicted, or from a previous server run).
  Clients that ignore it will silently miss events, exactly as before —
  the difference is that the gap is now announced.
- **`lcma.llm_provider` is removed**, replaced by the `lcma.llm` config
  block. The old field was never read by any code; a `LITHOS_LCMA__LLM_PROVIDER`
  env var now has no effect.
- **Salience decay stops at a floor** (`lcma.salience_floor`, default
  0.3) instead of decaying to zero, and a new usage signal enters
  reranking. Ranking on existing corpora shifts; run
  `lithos recalibrate-salience` once to lift collapsed rows.
- **Three idempotent startup migrations** on existing databases:
  `agents.archived_at` (+ a `lower(name)` index), `tasks.updated_at`
  (backfilled from `COALESCE(resolved_at, created_at)`, with legacy
  stamps normalised to the canonical serialised form), and the
  `llm_budget` / `edge_inference_log` ledger tables. Rolling back to
  0.4.0 is safe: the older code ignores the extra columns and tables.

### Added — LCMA phase 3 groundwork

#### Salience recalibration: decay-to-floor + usage signal (task e7d8ef60, #402)

Salience — the per-node retrieval-utility signal — had collapsed in prod
(avg 0.076, 86% of nodes ≤ 0.30) because a floor-less daily sweep decayed
every untouched node toward zero and nothing lifted the cold bulk corpus.
Time decay now bottoms out at `lcma.salience_floor` (explicit negative
feedback may still go below). A new non-decaying `usage_score` result
field — log-scaled retrieval frequency plus exponential recency, computed
from counters the rerank pre-fetch already reads, so zero extra queries —
separates learned quality (stored salience) from live popularity. The
previously hard-coded decay/reinforcement constants and composite weights
are now `LcmaConfig` fields (`salience_*`, `rerank_*`, `usage_*`). A new
`lithos recalibrate-salience` CLI command (idempotent; staging-first)
backfills collapsed databases. The pure math lives in `lithos/lcma/salience.py`.

#### LLM-synthesis substrate and typed-edge inference (task 7387506b, #405, #406)

The typed-edge graph was semantically empty in the 2026-07 audit (17%
coverage, two genuine relation types). Lithos can now ask an LLM to
adjudicate typed edges between semantically-close notes.

- **Config:** `lcma.llm` (`LlmConfig`) points at one generic
  OpenAI-compatible chat-completions endpoint — Ollama, llama.cpp, vLLM
  `/v1` shims locally, or a hosted API. **Enabled only when `base_url` is
  set; unset is a strict no-op.** `api_key` is a `SecretStr` hardened
  end-to-end: header-unsafe values are rejected with a value-free
  `ConfigurationError`, and transport errors are sanitised so the key
  can never echo into a log. Env: `LITHOS_LCMA__LLM__BASE_URL`,
  `..__MODEL`, `..__API_KEY`, `..__MAX_OUTPUT_TOKENS` (default 4096),
  `..__DAILY_TOKEN_BUDGET`, `..__MAX_CALLS_PER_DRAIN`,
  `..__CONFIDENCE_FLOOR`, `..__NEIGHBOUR_K`, `..__MIN_SIMILARITY` /
  `..__MAX_SIMILARITY`, `..__SNIPPET_CHARS`, `..__TIMEOUT_SECONDS`.
- **Client:** `lithos/lcma/llm.py`, async httpx, no SDK; JSON-object
  response format; every call recorded in a budget ledger (daily token
  budget) and an inference ledger (idempotency per node + doc version).
- **Worker:** the background enrich drain runs, per created/updated note,
  policy gate → top-K semantic neighbours → pure pre-filter (similarity
  band, same namespace, entity/tag overlap, already-inferred pairs
  skipped) → one LLM call adjudicating all candidates → judgements at or
  above the confidence floor written via `intake.assert_edge` as
  `provenance_type="inferred"`, `weight=confidence`,
  `evidence={rationale, model, confidence}`. Relation types:
  `supports`, `contradicts`, `refines`, `is_example_of`, `depends_on`,
  `analogy_to`. `contradicts` surfaces through the existing conflict
  machinery and is never auto-applied.
- **Cascade-proof:** inference writes stamp `origin=enrich`, so the
  worker drops its own `edge.upserted` events; edge-triggered drains
  never infer; edge writes do not bump the document version.
- **Metrics:** `lcma_llm_calls{outcome}`, token spend, and
  budget-exhaustion counters.

#### `lithos_retrieve` degraded-mode signal (task c36fbdb6, #396, #397)

Responses now always carry `degraded` (bool) and `failed_scouts` (the
canonical names of scouts that ran and raised), so a caller can tell
"one backend down, partial results" from "genuinely empty corpus".
Healthy retrieves gain `degraded: false, failed_scouts: []`; no existing
field changes. New alertable counter `lithos.lcma.scout.failures`,
labelled by scout — the signal that was missing during the 2026-06
ChromaDB outage that silently degraded semantic search for 33 minutes.
Scouts are now registered in one place with shared gating.

#### SSE replay-gap signal (task 87b45d1f, #400)

`EventBus.get_buffered_since` returned an empty list for three
indistinguishable cases — caught up, unknown id, evicted id — so a
reconnecting client that had missed events believed it was in sync. It
now returns `BufferedReplay(events, gapped)`, deciding continuity purely
by presence of the id in the ring, and the `GET /events` endpoint emits
`event: resync` (no `id:` line) when gapped. Presence-based detection is
what makes a reconnect across a server restart correct. New search and
graph conformance suites landed alongside.

### Added — coordination and knowledge API

#### Short id prefixes accepted everywhere; mutating responses echo the resolved id + title (task 83257ced)

Every tool parameter that takes a task or note id now also accepts an
**unambiguous short prefix** (min 6 chars, git-style) — the task lifecycle set,
task edges, `depends_on`/`parent_task_id`/`source_task_id` on create/spawn,
findings, and the note side (read/write/note_update/delete/related/node_stats).
Task prefixes resolve against tasks only, note prefixes against notes only.
An ambiguous prefix fails loudly with `ambiguous_id_prefix` carrying up to 5
`{id, title}` candidates — never a silent pick. Resolution is index-driven on
both sides (PK range scan / sorted in-memory mirror; SPECIFICATION §10.3).

Mutating responses now echo the resolved full id and title
(`task_id`/`title` on task mutations, endpoint ids+titles on edge upsert,
`title` on write/note_update, `id`/`title`/`path` on delete) so callers can
verify what they touched and transcripts retain full ids. Additive keys only.

Behaviour changes (full-length ids are always passed through unchanged):

- An id shorter than 6 chars matching nothing exactly is now `invalid_input`
  (was `task_not_found`/`doc_not_found`/`claim_failed`/silent-empty).
- A 6–35-char task id matching nothing is now `task_not_found` on every task
  tool — including claim/renew/release (were `claim_failed`/`claim_not_found`)
  and status/edge_list/children/finding_list (were silently empty).
- `lithos_write` with an unknown `id` now returns a `note_not_found` envelope
  (was an uncaught `FileNotFoundError` → protocol-level ToolError).

Review round (PR #412): id-bearing fields that *persist or filter* also
resolve, so a display prefix can never become silent wrong state. All are
lenient — an exact or unique-prefix hit resolves to the full id, ambiguity
errors, and anything else (forward references, free-form node ids and
correlation keys) passes through unchanged: `lithos_retrieve.task_id`,
`lithos_write.source_task`, `derived_from_ids`, finding `knowledge_id`, and
asserted-edge endpoints/filters/`winner_id`.

#### Tasks expose `updated_at`, a last-modified stamp bumped by every row write (#415)

Task records now carry `updated_at`, set on create (`= created_at`) and bumped
by every task-row mutation: any `lithos_task_update` (including metadata-only
merges — even `metadata={}`, which changes no keys but still writes the row),
complete/cancel (`= resolved_at`), and reopen. Claim operations
(claim/renew/release) touch only the claims table and never bump the stamp, so
lease heartbeats cannot masquerade as edits. The stamp is returned in
`lithos_task_get`/`lithos_task_list`/`lithos_task_status` (and the ready/
blocked/children pass-throughs), carried in the row-mutating event payloads
(`task.created`/`updated`/`completed`/`cancelled`/`reopened` — not
claimed/released), and echoed in the mutating tool responses so a writer can
record the exact stamp its own write produced without a racy re-read.
Consumers detecting "edited since X" compare stamps for equality. Existing
databases are migrated in place: the column is added at `initialize()` time
with a one-time backfill of `COALESCE(resolved_at, created_at)`.

#### `lithos_task_update` gains compare-and-set and tag set operations (task 6dbc3b80)

Two agents that each read a task, think, and write back can no longer
silently destroy each other's work. `lithos_task_update` accepts an optional
`expected_updated_at` token — the `updated_at` stamp from a prior read or
update echo, compared byte-for-byte. On mismatch nothing is written, no
event is emitted, and the tool returns the canonical error envelope
`{status: "error", code: "version_conflict", message, current_updated_at}`
so the caller can retry from the current stamp without re-reading. Without
the token, behavior is unchanged (last-writer-wins). Note the dialect
difference from the note side: notes keep their top-level
`status: "version_conflict"` write outcome; the task-side conflict is an
error envelope, matching every other task-tool failure.

Two review hardenings make the token collision-proof (#420): every
mutation of an existing task row — update (guarded or not), complete,
cancel, reopen — now reads the prior stamp inside its write transaction
and commits `max(now, prior + 1µs)` instead of the raw wall clock, so no
mutation can reuse or restore a previously issued stamp even when the
clock repeats or moves backward, and a CAS token is invalidated by ANY
intervening write; the committed stamp is echoed in responses and
events. And an idempotent startup migration normalizes legacy
SQLite-format `updated_at` values (`YYYY-MM-DD HH:MM:SS` from the #415
backfill) to the canonical serialized form, so tokens read from migrated
rows byte-round-trip instead of spuriously conflicting.

New `add_tags`/`remove_tags` parameters edit the tag list as set operations
(append without duplicates, drop removals, preserve order), applied
read-modify-write under `BEGIN IMMEDIATE` — so incremental tag edits from
stale reads compose instead of clobbering, which matters now that tags are
a dispatch mechanism (`trigger:` prefixes). They are mutually exclusive
with the wholesale `tags` replace and must not overlap each other.

#### Agent archive and name-collision warning (#423)

The agent roster could only grow: prod held 59 agents for ~15 actors,
most of them auto-registered by a single write and never seen again.

- **`lithos_agent_archive(id, agent)`** retires an agent from the roster.
  It keeps its history (tasks, claims, findings, access log still
  attribute to it) and stays readable via `lithos_agent_info`, but drops
  out of `lithos_agent_list` and the `agents` count in `lithos_stats`.
  Idempotent — the transition is one conditional `UPDATE`, so concurrent
  archives agree on a single stamp and a single event; self-archive
  follows the same contract. Unknown id → `{status: "error", code:
  "agent_not_found"}`. Emits `agent.archived` when newly archived.
- **Any activity resurrects.** Every write by the archived id
  (`ensure_agent_known`) and `lithos_agent_register` clear `archived_at`,
  so "archived" means exactly "no activity since archiving". Note this
  includes tool-name fallback ids such as `lithos_edge_upsert`, which an
  un-attributed edge upsert will bring back — by design.
- **`lithos_agent_list(include_archived=True)`** shows archived agents;
  every row (and `lithos_agent_info`) now carries `archived_at`, `null`
  while active. `lithos inspect agents --include-archived` matches.
- **`lithos_agent_register` warns on a name collision** — a non-empty
  `warnings` list names every other active agent with the same `name`
  (case-insensitive, via an expression index on `lower(name)`).
  Registration never blocks: distinct running instances legitimately
  share a display name. The response gains `warnings: []` on success.
- Startup migration adds `agents.archived_at` and the name index to an
  existing coordination.db, idempotently.

#### `lithos_read` returns the note's `path`

`lithos_read` omitted the note's path — neither top-level nor in
`metadata` — while `lithos_list`, `lithos_write` and `lithos_delete` all
report it, so a client reading by id could not learn where a note lives
(lithos-loom surfaced this as `path=""`). The response now carries
`path` beside `id` and `title`: the file path relative to `knowledge/`,
identical to what `lithos_list` returns, for reads by `id` and by `path`
alike. Additive — no existing key changes.

### Fixed

- **Bare YAML dates in frontmatter broke indexing (#407, #411).** PyYAML
  resolves an unquoted `created: 2026-07-30` to `datetime.date`. On 0.4.0
  any note carrying one under a non-reserved key was silently dropped
  from search (one reporter lost ~25% of a vault); after the corpus-index
  extraction the exception escaped `rebuild()` and stopped the server
  booting, and a bare date in `tags:` poisoned the whole Tantivy rebuild.
  Both YAML ingestion points now normalise `date`/`datetime` values to
  ISO-8601 strings, and the metadata bucket key is total (`default=str`),
  so hand-edited frontmatter can no longer abort the startup scan.
- **Docker: `LITHOS_LCMA__LLM__*` env vars are forwarded into the
  container (#409, #410).** The compose `environment:` whitelist never
  passed them, so LLM synthesis stayed silently disabled in
  compose-managed deployments. The whole family is now passed through;
  `LithosConfig` gained `env_ignore_empty=True` so blank pass-through
  defaults are treated as unset rather than failing int/float parsing.
- **`lcma.llm.max_output_tokens` default raised 1024 → 4096 (#410).**
  Reasoning models spend completion budget on hidden reasoning before
  emitting text; a production-shaped prompt consumed 979 of the 1024-token
  cap and 20 of 23 staging calls failed. The cap is protective, not a
  spend target, so the change costs nothing on non-reasoning models.
- **Guardrail review follow-ups (#380)** and three guardrail-kit
  adoptions (1.0.0, 1.3.2, 1.4.0 — #381, #382, #383).

### Internal — 2026-07 architecture-cleanup roadmap

No MCP behaviour change from any of these; each was drift-checked against
the generated architecture docs.

- **MCP tools extracted into `lithos.tools`** (#372, #373, #374): agents,
  memory/edges, findings/stats, notes, read/search and task tools each
  live in their own module; the monolithic `_register_tools` is gone.
  Tests migrated onto the shared `tests.helpers.call_tool` seam (#375).
- **Enrich, quarantine and supersede note writes route through Corpus
  intake** (#384); **edge-write policy consolidated** with a
  non-spoofable enrich loop-break (#385); **`derived_from` projection
  consolidated into `ProvenanceProjection`** (#386).
- **One composition root** for the component graph (#388); the Core-tier
  reconcile peer retired, completing ADR-0001 (#389).
- **Frontmatter codec** (`lithos.frontmatter_codec`, #390) and the
  **derived corpus index** (`lithos.corpus_index`, #393) extracted out of
  `KnowledgeManager`; provenance BFS given an owner and the scouts'
  cached-meta view collapsed (#394).
- **Shared `AsyncSqliteStore` base** for the edge and stats stores (#398);
  task-feedback validate/apply moved onto `CognitiveMemory` (#399).
- **Agent registry extracted from `coordination.py`** (#425): the `agents`
  table DDL, the `Agent` dataclass and the four registry operations now
  live in `lithos.agent_registry`; `CoordinationService` keeps thin
  delegators with the original signatures. The SQLite datetime codec
  moved to `lithos.sqlite_datetime` as public `parse_datetime` /
  `format_datetime`. Takes `coordination.py` from 3005 lines to 2823,
  clear of the 3050 stop-loss it was about to breach.
- **Architecture guardrails extended** (#379): `docs/generated/` grows
  from 2 artifacts to 21 — metrics with budgets and trends, the MCP tool
  catalog, C4 container and per-component views — all generated by
  `tests/guardrail/` and drift-checked in CI. Zero new runtime
  dependencies.
- **CI publishes only the agent-skill plugins touched by a push** (#414);
  the lithos skill went through 0.3.0 → 0.4.1 across this release
  (short-id backstop, `updated_at` change detection, agent-id lookup
  convention, task graph and 0.4.0 envelopes, the read response shape).
- **Dependency bumps** via Dependabot, including anyio 4.14.2.

---

## [0.4.0] — 2026-07-06

### BREAKING — MCP error envelopes normalized (0.4.0)

Every tool **failure** now uses one canonical envelope:

```json
{"status": "error", "code": "<stable_snake_case>", "message": "<sentence>"}
```

Validation failures carry the reserved code `invalid_input` — including
previously-raising paths such as unparseable datetime filters
(`lithos_list.since`, `lithos_agent_list.active_since`,
`lithos_finding_list.since`). Error envelopes no longer include a `warnings`
key. Precisely: every failure a handler can anticipate (validation and
operational) is returned as this envelope; protocol-level `ToolError`s remain
only for requests rejected by MCP schema validation before the handler runs,
and for unexpected internal exceptions (bugs).

Before / after for a rejected write:

```json
// before
{"status": "invalid_input", "message": "ttl_hours must be ...", "warnings": []}
// after
{"status": "error", "code": "invalid_input", "message": "ttl_hours must be ..."}
```

**What changed, by tool family:**

- `lithos_write` / `lithos_note_update`: boundary-validation rejections and the
  `invalid_input` / `content_too_large` / internal-`error` outcome passthroughs
  now use the canonical envelope (internal errors carry `code: "internal_error"`).
- `lithos_list`, `lithos_task_list`, `lithos_task_ready`, `lithos_task_blocked`:
  `metadata_match` validation rejections now use the canonical envelope.
- Everything else was already canonical and is unchanged.

**What deliberately did NOT change** — actionable write outcomes keep their own
top-level `status` (they carry payloads agents act on): `version_conflict`
(with `current_version`; read-merge-write retry loops keep branching on it),
`duplicate`, `slug_collision`, `path_collision`, all success envelopes, and the
`{"success": ...}` claim/release shapes. Error `code` strings are also
unchanged (`doc_not_found` vs `note_not_found` etc. keep their spellings).

**Migration:** replace any `status == "invalid_input"` branch with
`status == "error" and code == "invalid_input"`, and stop reading `warnings`
off error responses. This partially reverses the earlier pre-0.3 change that
promoted every write-error code to a top-level status — that promotion made
`status` an open set an agent had to enumerate just to detect failure; the
line now sits at "actionable outcome vs failure". Permitted by the pre-1.0
compatibility policy in `SPECIFICATION.md §1.4`.

### Added

- **Free-form metadata on knowledge notes (#305).** `lithos_write` accepts a `metadata` object of arbitrary key/value pairs (scalars or lists), persisted to YAML frontmatter via `KnowledgeMetadata.extra`. `lithos_read` returns it as `metadata.extra`; `lithos_list` includes it on each item. Update semantics mirror `tags`: omit/`null` preserves, `{}` clears, a non-empty dict is an additive per-key merge (per-key `null` deletes). Keys that collide with reserved frontmatter fields are rejected with the canonical error envelope (`status="error"`, `code="invalid_input"` — see the BREAKING entry above).
- **`metadata_match` filter on `lithos_list` and `lithos_task_list` (#306).** Filter by free-form metadata: AND across keys, where each `key: q` matches records whose stored value equals `q` or is a list containing `q` (e.g. `github_repos: ["org/a","org/b"]` matches `{"github_repos": "org/a"}`). Query values are scalars; matching is type-sensitive. `lithos_list` resolves it through an in-memory inverted index (no full scan); `lithos_task_list` pushes it into SQLite via `json_extract`/`json_each`. As part of this, `lithos_list`'s existing equality filters (`tags`, `author`) are now index-backed too.
- **`lithos_task_list` `with_claims` flag.** When set to `true`, each task in the response includes its active (non-expired) claims inline as a `claims` array (same shape as `lithos_task_status`). Defaults to `false`, so existing payloads are unchanged. Lets list views avoid an N+1 of `lithos_task_status` calls. Implementation issues a single batched `WHERE task_id IN (...)` query rather than one per task.

### Changed

- **`lithos_write` error envelopes are now canonical top-level statuses.** Each error code surfaces as `status="<code>"` (e.g. `status="slug_collision"`, `status="invalid_input"`) instead of `status="error"` plus a separate `code` field. Affects `slug_collision`, `invalid_input`, `content_too_large`, and `version_conflict`. `status="error"` is retained as a generic fallback. **MCP-boundary breaking change** — clients that dispatched on `(status, code)` need to dispatch on `status` alone. Permitted by the pre-1.0 compatibility policy in `SPECIFICATION.md §1.4`.
- **`slug_collision` envelopes now include a structured `existing_id` field** (the colliding document UUID) so clients can avoid scraping the message string.
- `WriteResult.error_code` is removed; `WriteResult.status` is the single source of truth and is one of `created` / `updated` / `duplicate` / `invalid_input` / `content_too_large` / `version_conflict` / `error`.

---

## [0.2.0] — 2026-04-12

### Added — LCMA MVP1 (Layered Cognitive Memory Architecture)

- **`lithos_retrieve`** — new cognitive retrieval tool that orchestrates parallel scouts
  (vector, lexical, provenance, task-context) with merge-and-normalize, Terrace 1
  reranking, and audit receipt logging on every call. Returns `reasons`, `scouts`,
  `salience`, `temperature`, `terrace_reached`, and `receipt_id` per result.
- **`lithos_edge_upsert`** — create or update typed edges in `edges.db`.
  Upsert key is `(from_id, to_id, type, namespace)`.
- **`lithos_edge_list`** — query edges from `edges.db` by optional filters
  (`from_id`, `to_id`, `type`, `namespace`).
- **`lithos_write` LCMA fields**: `note_type`, `namespace`, `access_scope`,
  `summaries`, `schema_version` — all optional and additive; existing documents
  are unaffected.
- **`edges.db`** and **`stats.db`** base tables for graph and statistics storage.
- **Receipts logging** — every `lithos_retrieve` call writes an audit receipt
  (`rcpt_*`) for full observability.
- **`lithos_health` removed from MCP surface** — health checks are now HTTP-only
  at `GET /health`.

### Added


- **Structured JSON logging** (`logging_config.py`): all log output is now
  emitted as single-line JSON objects with fields `timestamp`, `level`,
  `logger`, and `message`.  OTEL trace-context extras (`otelTraceID` etc.)
  appear automatically when tracing is active.
  - New dependency: `python-json-logger >= 3.0`.
  - Escape hatch: set `LITHOS_LOG_FORMAT=text` to revert to plain human-readable
    output (useful for local dev or stdio transport).
  - Timestamps always use ISO 8601 with colon-separated UTC offset (`+00:00`).

### Breaking Changes

- **`lithos_semantic` MCP tool removed.** Use `lithos_search` with `mode="semantic"`
  for pure semantic search, or the new default `mode="hybrid"` for best results.
- **`lithos_search` now defaults to hybrid mode.** Existing callers that relied on
  `lithos_search` for full-text-only results will now receive hybrid (BM25 + semantic
  RRF) results instead. Pass `mode="fulltext"` explicitly to restore the previous
  behaviour.
- **`similarity` key renamed to `score` in search results.** Callers migrating from
  `lithos_semantic` that read `result["similarity"]` must update to `result["score"]`.
  All three modes (`hybrid`, `fulltext`, `semantic`) now use a unified `score` field.

### Added

- `lithos_search` now accepts a `mode` parameter (`fulltext` | `semantic` | `hybrid`,
  default: `hybrid`).
- Hybrid search mode merges Tantivy (BM25) and ChromaDB (cosine similarity) results
  using Reciprocal Rank Fusion (RRF, k=60) for improved ranking quality.
- Unknown `mode` values now return a structured `{"status": "error", "code": "invalid_mode", ...}`
  dict instead of raising a `ValueError`.

### Fix: `lithos_read` returns structured error on missing document (issue #102)

Previously, `lithos_read` propagated a raw `FileNotFoundError` as an
MCP-level exception when the requested id or path did not exist.  This
is the most common failure path and a predictable condition, not a crash.

`lithos_read` now catches `FileNotFoundError` and returns a structured
error envelope:

```json
{ "status": "error", "code": "doc_not_found", "message": "..." }
```

### Fix: Consistent error envelopes across all tools (issue #85)

Error handling was inconsistent across tool categories:

- Coordination tools (`lithos_task_claim`, `lithos_task_renew`,
  `lithos_task_release`, `lithos_task_complete`) returned `{ success: false }`
  on failure.
- `lithos_delete` returned `{ success: false }` when the document was not found.

All failure paths now return a standard error envelope:

```json
{ "status": "error", "code": "...", "message": "..." }
```

| Tool | Error code |
|------|-----------|
| `lithos_delete` (not found) | `doc_not_found` |
| `lithos_task_claim` (task missing/closed/conflict) | `claim_failed` |
| `lithos_task_renew` (no active claim) | `claim_not_found` |
| `lithos_task_release` (no matching claim) | `claim_not_found` |
| `lithos_task_complete` (task missing/not open) | `task_not_found` |

**Breaking:** callers that checked `result.get("success") == False` on
coordination tools must be updated to check `result.get("status") == "error"`.
Success paths are unchanged.

### Breaking: `agent` is now required on `lithos_delete` (issue #80)

For audit-trail consistency, `agent` was optional on `lithos_delete` while
it was required on every other mutation tool (`lithos_write`,
`lithos_task_create`, `lithos_task_claim`, `lithos_task_complete`,
`lithos_finding_post`).

`agent` is now a **required** parameter on `lithos_delete`.  Callers that
omit it will receive a `TypeError` from the MCP layer.  This is a breaking
change intended to land before v1.0.
### Schema change — `version` field in frontmatter (issue #45)

PR #55 adds optimistic locking via a `version` integer field in the YAML
frontmatter of every knowledge document.

**Existing documents** written before this change will be treated as
`version: 1` on first read (the field defaults to `1` when absent). No
migration is needed; the field is added automatically the next time a
document is updated.

**New documents** will have `version: 1` written into their frontmatter
at creation time.

**Breaking (update calls only):** the `lithos_write` MCP tool now accepts
an optional `expected_version` parameter.  If provided and the document's
current version does not match, the call returns a `version_conflict`
error.  Callers that do not pass `expected_version` are unaffected.


