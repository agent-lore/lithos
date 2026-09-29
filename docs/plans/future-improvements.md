# Future Improvements (Deferred)

Known gaps and ideas not addressed by any active implementation plan.
Candidates for a plan or a tracker task; not blockers for anything scheduled.
Rewritten 2026-09-29 from the state review (`docs/reviews/2026-09-state-review.md`);
items that shipped since the 2026-06 version (conflict resolution, quality
signals, namespace fields, the task graph Phases 1–3) were removed.

Anything with a consumer should become a `lithos-core` tracker task; this
file is for ideas that do not yet have one.

## 1. Task graph Phase 4 (unscheduled)

Carried from `archive/task-graph-coordination-extension.md` (Phases 1–3
delivered, PRs #342/#343/#356/#357):

- a first-class `priority` column (today an inheritable metadata key only)
- epic close rules (no rule today; `SPECIFICATION.md` says so)
- `lithos_task_prime` — a gated context brief on claim; see
  `lcma-connected-knowledge.md` K5 for the current design
- further edge types (`relates_to`, `duplicates`, …) only when a consumer asks

## 2. Task-tool ergonomics surfaced by real callers

- `lithos_task_list` lacks `project` and `limit` while `task_ready` /
  `task_blocked` accept both; callers guess and hit a FastMCP validation
  error that no metric counts.
- A bulk subtree read (`lithos_task_children(task_ids=[...])` or an epic
  roll-up) so Lens stops fanning one call per epic per refresh (533k calls in
  30 days, 83% of prod traffic); Lens should also refresh from SSE `task.*`
  events rather than a timer.
- `lithos_list` server-side ordering (`order_by=updated_desc`, task `e0e31654`).

## 3. Knowledge-write ergonomics

- **Batch existence check.** 97% of influx `lithos_write` calls are
  re-submissions of known URLs. A `lithos_cache_lookup(source_urls=[...])`
  or `lithos_exists` batch call is the first real argument for anything in
  `deferred/bulk-write-v3.md`; the full batch write stays deferred.
- **Source-stable identifiers** (arxiv id, DOI) as a dedup key beside
  `source_url` (GitHub #222).
- **Bulk import / `validate --fix` for foreign vaults**: walk a directory,
  validate frontmatter against `KnowledgeMetadata`, repair missing ids and
  defaults, write conformant files, reindex. Natural home: the `admin`
  group in `cli-admin-client-split.md`.

## 4. Attribution and observability

- 70% of `lithos_read` calls are unattributed because `agent_id` is optional;
  either require it on read/search/list or derive it from the session's
  registered agent.
- `otel_lithos_tool_errors_total` has never incremented, so no error-rate
  series exists; FastMCP validation errors bypass the JSON logger and the
  counter.
- LLM call-duration histogram buckets stop at 10 s (mean is 16.5 s); add
  buckets to 120 s.
- `gen_ai.*` spans on LLM calls (the deferred Phase 3 of
  `archive/otel-plan.md`, now that `lcma/llm.py` exists).
- Docker `json-file` log driver has no `max-size` on prod/staging compose
  (Lens has 10m×5).

## 5. Integrity safeguards

- Chroma coverage drift alert (drift detection is Tantivy-only today) and a
  scheduled reconcile safeguard — the re-scoped half of task `97cd00bb`.
- ADR-0006 slices 2–3: consolidation and reinforcement edges still write to
  `edge_store` directly rather than through intake; `lithos_conflict_resolve`
  dual-writes edges and notes without the promised atomicity ADR. Single
  process, no incident; do it when the write path is next touched.

## 6. Small runtime fixes

Standalone defects get fixed when found, not queued behind roadmap phases.
