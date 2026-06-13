# Task Graph Coordination Extension

Status: Proposal (revised after review of PR #339 — trimmed MVP edge types, removed dual-source migration compatibility, made `parent_child` purely structural, made gate resolution explicit, deduplicated the tool surface)

Audience: Lithos maintainers and agent-tooling implementers

Purpose: extend Lithos's existing task coordination model so it can act as a first-class issue tracker and ready-work scheduler for agents, while remaining consistent with the current knowledge-first architecture.

---

## 1. Summary

Lithos already provides a solid lightweight coordination layer:

- task creation and listing
- TTL-based aspect claims
- findings attached to tasks
- project association via tags and `metadata.project`

That is enough for shared task capture and collision avoidance. It is not enough for robust agent scheduling across long-running, dependency-heavy work.

This proposal adds a **task graph** to `coordination.db` and a small set of new MCP tools that let agents answer:

- What work is ready now?
- What is blocked, and by what?
- What task did this follow-on work come from?
- What children belong to this epic?
- What is waiting on a human, CI, timer, or external dependency?

The design intentionally builds on current Lithos conventions such as:

- `metadata.depends_on`
- `metadata.priority`
- `metadata.parallelizable`
- `metadata.project`

The extension makes those patterns first-class, queryable, and consistent across agents.

---

## 2. Goals

### 2.1 Primary Goals

1. **First-class dependency tracking** for tasks, not just ad hoc metadata.
2. **Deterministic ready-work selection** so agents can resume after session loss without reparsing prose.
3. **Project-local issue graph semantics** without introducing a separate issue tracker product.
4. **Clean one-time migration** from today's task metadata conventions, with no ongoing compatibility layer.
5. **Knowledge-aware scheduling** that remains integrated with Lithos retrieval and findings.

### 2.2 Non-Goals

1. Replacing the existing knowledge graph in `edges.db`
2. Turning `coordination.db` into a general-purpose workflow engine
3. Enforcing distributed locks beyond the current claim mechanism
4. Introducing a hard security boundary between agents
5. Replacing GitHub Issues or external trackers as the public-facing issue system

---

## 3. Current Gaps

Today, many useful scheduling fields are stored only in `tasks.metadata` by convention:

- `priority`
- `depends_on`
- `parallelizable`
- `phase`
- `blocked_on`

This creates five problems:

1. **No native ready queue** — Lithos cannot compute "all open tasks whose blockers are satisfied".
2. **No blocker explanation surface** — agents have to interpret metadata themselves.
3. **No validation** — malformed task IDs, cycles, and inconsistent dependency types are not prevented.
4. **No hierarchy** — epics, subtasks, and follow-on tasks are flattened into one table.
5. **No external wait model** — waiting on CI, delivery, human approval, or time is represented as prose.

These are platform-model gaps, not just missing UI affordances.

---

## 4. Design Overview

Add a **task relation layer** inside `coordination.db`.

The key idea is simple:

- `tasks` remains the canonical row for task identity and lifecycle.
- `claims` remains the canonical lease/ownership mechanism.
- `findings` remains the canonical task-local event stream.
- New **task edges** define ordering, hierarchy, and follow-on relationships.
- Optional **gates** model waiting on external conditions.

This preserves Lithos's current separation of concerns.

---

## 5. Data Model

## 5.1 Task Types

Extend tasks with a first-class `task_type` field.

Allowed values:

- `task` — default discrete unit of work
- `epic` — parent roll-up container
- `gate` — external wait condition

There is deliberately **no `subtask` type**. "Child" is a purely relational property, so it lives on the `parent_child` edge (§5.2), not on the node: a child is any task that is the `to` end of a `parent_child` edge. Encoding the relationship as a node attribute as well would create two sources of truth that can drift. (`message`, an async coordination artifact, was likewise considered and deferred until a concrete use case exists.)

Storage:

- new `task_type TEXT NOT NULL DEFAULT 'task'` column on `tasks`, added by schema migration
- the column is the only source of truth; `metadata.task_type` is not read

**Write-validation is phase-gated, mirroring the edge-type rule in §5.2.** The column defaults to `'task'` and physically holds any string, but a value is only *accepted on write* once the phase implementing its semantics has shipped: Phase 1 accepts only `task`; `epic` is accepted from Phase 2 (when roll-up and ready-exclusion semantics land); `gate` from Phase 3. This stops agents creating a `gate` that gates nothing, or an `epic` that `ready` would wrongly surface as workable.

## 5.2 Task Edges

Add a new `task_edges` table:

```sql
CREATE TABLE task_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_task_id TEXT NOT NULL,
    to_task_id TEXT NOT NULL,
    type TEXT NOT NULL,
    metadata JSON,
    created_by TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (from_task_id) REFERENCES tasks(id),
    FOREIGN KEY (to_task_id) REFERENCES tasks(id),
    UNIQUE(from_task_id, to_task_id, type)
);

CREATE INDEX idx_task_edges_from ON task_edges(from_task_id, type);
CREATE INDEX idx_task_edges_to ON task_edges(to_task_id, type);
```

The indexes are required, not optional: ready/blocked queries join `task_edges` against open tasks and must stay sub-linear. Cycle detection on write must also use a bounded traversal over these indexes.

Semantics:

- `from_task_id` is the source task
- `to_task_id` is the target task
- `type` determines scheduling semantics

MVP edge types. An edge type is only accepted on write once the phase that implements its readiness semantics has shipped — agents must never be able to write a blocking edge whose meaning is not yet implemented:

Accepted from Phase 1:

- `blocks`
  Meaning: `to_task_id` cannot be considered ready until `from_task_id` reaches `completed`. A blocker that ends in a **non-`completed` terminal state** (`cancelled`) does **not** satisfy the edge — `to_task_id` stays blocked (surfaced via `lithos_task_blocked` as `blocker_unsatisfiable`; see §6.1, §8), because the work the dependent was waiting on never happened. Resolving readiness on "predecessor is merely not `open`" would instead let a cancelled blocker spuriously ready its dependents — a correctness bug for strict-sequential chains (see §12.4). Blocking.
- `parent_child`
  Meaning: `from_task_id` is the parent; `to_task_id` is the child. Purely structural — it never blocks the child. The hierarchy is a **forest**: a task has at most one parent (a second `parent_child` edge to the same child, from a different parent, is rejected with `parent_exists`), and it is kept acyclic (an edge making a task its own ancestor is rejected with `cycle` — same bounded traversal as `blocks`, in the descendant direction). Epic roll-up *close* rules (e.g. a parent cannot close while children are open) operate in the reverse direction and are deferred to Phase 4.
- `discovered_from`
  Meaning: `to_task_id` was discovered during execution of `from_task_id`. Non-blocking; exists to support `lithos_task_spawn` provenance.

Accepted from Phase 3 (alongside gate readiness semantics):

- `waits_on_gate`
  Meaning: `to_task_id` is blocked by the gate task `from_task_id` (same direction as `blocks`: `from_task_id` is the blocker). `to_task_id` cannot be ready until the gate is resolved — i.e. the gate is `completed`, or for an `open` `timer` gate, `ready_at <= now` at query time. A **cancelled** gate is *unsatisfiable* (it never resolves; the waiter is surfaced as `blocker_unsatisfiable`), mirroring the cancelled-`blocks` rule (§8/§12.4) — "proceed anyway" is expressed by *completing* the gate, not cancelling it. Blocking.

Deferred edge types (add only when something concretely consumes them, since each carries implied semantics and validation — e.g. terminal-state behavior for duplicates):

- `caused_by`
- `validates`
- `relates_to`
- `duplicate_of`
- `superseded_by`

Because `type` is a plain TEXT column, adding these later is a validation-list change, not a schema migration.

## 5.3 Gates

Represent gates as ordinary tasks with `task_type='gate'` plus structured metadata.

Required gate metadata:

- `gate_type`: `human | timer | ci | pr | external_task`

Optional gate metadata by type:

- `timer`: `ready_at`
- `human`: `approval_required_from`, `reason`
- `ci`: `provider`, `run_id`, `required_status`
- `pr`: `provider`, `repo`, `pr_number`, `required_state`
- `external_task`: `system`, `external_id`, `required_state`

Why gates as tasks:

- fits current task lifecycle
- reuses claiming, status, and findings
- keeps scheduling state inspectable through existing task tools

### Gate resolution model

**Lithos never polls external systems.** It is a passive MCP server; gates are resolved as follows:

- An `open` `timer` gate is evaluated at query time: resolved once `ready_at <= now`. No state change is required. `ready_at` is validated and normalized to a canonical UTC second-precision ISO string at creation so the ready and blocked queries compare it identically.
- All gate types are resolved by an agent (or a hook/script acting as one) **completing** the gate task via `lithos_task_complete` when it observes the external condition is met.
- **Cancelling** a gate means the awaited condition will *not* be met: the gate is unsatisfiable and its waiters become `blocker_unsatisfiable` (not readied). complete = condition met → release; cancel = condition won't be met → intervene.

`gate_type ∈ {human, timer, ci, pr, external_task}` is required at creation (strict validation). The other gate metadata (`provider`, `run_id`, `pr_number`, etc.) exists so the resolving agent knows what to check — it does not imply Lithos watches those systems. Anything that polls CI or PR state lives outside Lithos.

---

## 6. MCP Tool Additions

## 6.1 New Tools

### `lithos_task_edge_upsert`

Create or update a typed relation between two tasks.

Arguments:

- `from_task_id: str`
- `to_task_id: str`
- `type: str`
- `agent: str`
- `metadata: dict | None = None`

Returns:

- `{ success: true }`
- or `{ status: "error", code, message }`

Validation:

- both tasks must exist
- `type` must be an edge type accepted in the current phase (§5.2); deferred types and not-yet-shipped types (e.g. `waits_on_gate` before Phase 3) are rejected
- self-edges are rejected
- cycles in blocking edges are rejected via a bounded traversal at write time

### `lithos_task_edge_list`

List incoming/outgoing edges for one task, optionally filtered by type.

Arguments:

- `task_id: str`
- `direction: "incoming" | "outgoing" | "both" = "both"`
- `types: list[str] | None = None`

Returns:

- `{ edges: [...] }`

### `lithos_task_ready`

Return open tasks with no unsatisfied blocking edges (every blocking predecessor `completed`) and no unresolved gates.

Arguments:

- `project: str | None = None`
- `tags: list[str] | None = None`
- `metadata_match: dict | None = None`
- `limit: int = 50`
- `with_claims: bool = True`

Returns:

- `{ tasks: [...] }`

Behavior:

- only `status='open'`
- excludes tasks with an unsatisfied blocking predecessor — one not yet `completed` (still `open`, or terminal-but-not-completed i.e. `cancelled`; see §8)
- excludes tasks blocked by unresolved gates
- **never excludes by claim.** When `with_claims` (default), each task's active claims are *attached* inline; the tool does not filter claimed tasks out. Collision-correctness already comes from the atomic claim in `claim_task`, and claims are per-aspect (a task can have one aspect claimed while another is free), so a task-level "claimed" exclusion would both be ill-defined and hide legitimately-available parallel work. The picking agent decides what "taken" means. (The parameter is named `with_claims` to match `lithos_task_list`.)

### `lithos_task_blocked`

Return open tasks that are not ready, with structured blocker reasons.

Arguments:

- same filter surface as `lithos_task_ready`

Returns:

- `{ tasks: [{..., blockers: [...]}] }`

Blocker entries should include:

- `kind`: `task | gate | cycle | blocker_unsatisfiable`
- `task_id` or gate reference
- `type`
- `status` — for `task` and `blocker_unsatisfiable` kinds, the blocking predecessor's current status (`open` / `cancelled`), so a client can distinguish a still-running blocker ("not yet") from a permanently-failed one ("needs intervention")
- `message`

There is no `external` blocker kind: external waits are always represented as gate tasks (§5.3). Every blocker is therefore a `task` (an `open` predecessor — the dependent is merely waiting on in-progress work), a `blocker_unsatisfiable` (a predecessor that ended `cancelled`, so the dependent can never become ready without intervention — see §8 and §12.4), a `gate`, or a `cycle` error from the backfill (§8). `cycle` blockers carry the task IDs forming the cycle in `message`; a `blocker_unsatisfiable` blocker carries the cancelled predecessor in `task_id` (with its `status`), keeping `message` human-readable.

### `lithos_task_spawn`

Create a follow-on task linked to an existing source task.

Arguments:

- `source_task_id: str`
- `title: str`
- `agent: str`
- `description: str | None = None`
- `relation_type: "discovered_from" | "blocks" = "discovered_from"`
- `inherit_project: bool = True`
- `inherit_tags: bool = True`
- `inherit_context: bool = True`
- `metadata: dict | None = None`

Returns:

- `{ task_id: str }`

Behavior:

- inheritance flags copy from the source: `inherit_project` → `metadata.project`; `inherit_tags` → the source's tags; `inherit_context` → a fixed allow-list of scheduling-convention metadata keys (`priority`, `parallelizable`, `phase`). Forbidden keys (`depends_on`/`blocked_on`) are never inherited, and an explicit `metadata` arg overrides inherited values. The spawned task is always `task_type='task'`.
- creates the relation edge automatically, always with the source task as `from_task_id` and the spawned task as `to_task_id`:
  - `discovered_from`: source → spawned (the spawned task was discovered during the source task)
  - `blocks`: source → spawned (the spawned task is blocked until the source task is `completed`; a `cancelled` source leaves it `blocker_unsatisfiable` — see §8). Spawning a task that blocks its source is not supported; use `lithos_task_edge_upsert` explicitly for that.

### `lithos_task_children`

Return child tasks for a parent/epic.

Arguments:

- `task_id: str`
- `recursive: bool = False`
- `include_closed: bool = False`

Returns:

- `{ tasks: [...] }`

### Considered and deferred: `lithos_task_prime`

An earlier draft proposed a `lithos_task_prime` tool returning a combined handoff payload (task record, claims, edges, blockers, recent findings, retrieval context). It is deferred: it largely composes data already reachable via `lithos_task_get`-style reads, edge listing, and retrieval, and every MCP tool costs context in each agent's window. Revisit in Phase 4 if agents demonstrably need a single bootstrap envelope.

## 6.2 Existing Tool Enhancements

### `lithos_task_create`

Enhancements:

- accept `task_type`
- accept `parent_task_id`
- accept `depends_on` as a convenience field

Behavior:

- if `parent_task_id` is passed, create a `parent_child` edge
- if `depends_on` is passed, create `blocks` edges from each predecessor

### `lithos_task_complete`

Enhancements:

- optionally emit newly unblocked tasks in response payload

(Duplicate/supersession terminal-state behavior is deferred along with those edge types.)

### `lithos_task_list`

Enhancements:

- optional `task_type` filter

Ready/blocked filtering deliberately does **not** get `ready_only`/`blocked_only` flags here: `lithos_task_ready` and `lithos_task_blocked` are the single surface for readiness queries (the blocked view returns a different shape, with blocker explanations). One surface per question keeps the tool contract unambiguous.

---

## 7. Migration Strategy

**Single source of truth from day one.** Lithos is local-first; there is no fleet of remote deployments that needs a compatibility window. The migration is therefore a one-time backfill performed as part of the schema migration that creates `task_edges`, after which `task_edges` is the only thing the scheduler reads. There is no transition period, no virtual-edge projection from metadata, and no precedence rules between old and new representations.

## 7.1 One-Time Backfill

As part of the schema migration:

1. Scan open tasks
2. Read `metadata.depends_on` and `metadata.blocked_on`
3. Create canonical `blocks` edges (skipping references to nonexistent task IDs, which are logged)
4. Record a migration marker in edge metadata:
   - `{"migrated_from": "metadata.depends_on"}`
5. Cycles found during backfill are surfaced as explicit errors; the **tasks participating in the cycle** are excluded from `ready` results and reported as blocked with a cycle error until the cycle is repaired (see §8). The cyclic edges themselves are kept, never silently dropped — ignoring them would make the cycle's tasks incorrectly appear ready.

This makes existing projects that already use dependency metadata immediately scheduler-aware.

## 7.2 After the Backfill

- `metadata.depends_on` and `metadata.blocked_on` are no longer read by anything, and the write path is closed too: once edges are canonical, `lithos_task_create` and `lithos_task_update` **reject metadata writes containing those keys** with an error directing the caller to `depends_on` on `lithos_task_create` (which creates edges) or `lithos_task_edge_upsert`. Without this, the additive metadata write path would let agents keep recreating misleading stale scheduling state. Old task rows may retain the keys as inert data.
- `metadata.priority` remains the priority convention in MVP (see §9); it is advisory, not structural, so it stays metadata until Phase 4.
- `metadata.parallelizable` likewise remains advisory metadata.

---

## 8. Ready-Work Semantics

MVP readiness rule:

A task is ready when all of the following are true:

1. `status == "open"`
2. the task is a directly-workable unit — i.e. not a `gate` (and, once Phase 2 ships the `epic` type, not an `epic`: you execute an epic's children, not the epic itself). The readiness predicate uses a forward-compatible `task_type NOT IN ('gate', 'epic')` guard from Phase 1; the excluded types simply cannot be created yet until their phase lands.
3. every incoming blocking edge is **satisfied** — for a `blocks` edge that means the predecessor is `completed` (a predecessor still `open`, or terminal-but-not-`completed` i.e. `cancelled`, leaves the edge unsatisfied; see the blocker-failure policy below)
4. every incoming `waits_on_gate` edge is **satisfied** — the gate is `completed`, or an `open` `timer` gate whose `ready_at <= now`. A `cancelled` gate (cancelled wins over any timer) leaves the edge unsatisfied → `blocker_unsatisfiable`. (Conditions 3 and 4 are one SQL predicate — see the implementation; `blocks` and `waits_on_gate` are the unified dependency set.)

Claims do **not** enter the readiness predicate: a claimed task is still ready. `lithos_task_ready` *attaches* active claims (when `with_claims`) but never excludes by them — see §6.1 for why (atomic-claim correctness + per-aspect claims).

MVP blocked rule:

A task is blocked when:

- it is open
- and at least one blocking predecessor is unsatisfied (still `open`, or `cancelled` — see below), or an unresolved gate applies

Blocker-failure policy:

- a `blocks` predecessor that ends `cancelled` (terminal but not `completed`) leaves every dependent **permanently** blocked — the awaited work will never complete. Such dependents are excluded from `ready` and surfaced via `lithos_task_blocked` with a `blocker_unsatisfiable` blocker carrying the cancelled predecessor's id + status, so an agent can decide to re-open/re-route the predecessor or cancel the stranded subtree. The edge is retained (never silently dropped) — exactly as for backfill cycles below — because dropping it would make the dependent spuriously `ready`.
- this keeps Lithos **passive**: it does not auto-cancel or auto-reroute the stranded dependents; it refuses to call them ready and explains why, leaving the policy decision to the agent/orchestrator.

Cycle policy:

- cycles in blocking edges are rejected on write, using a bounded traversal over the `task_edges` indexes (never a full-table walk)
- for cycles found during the one-time backfill, the tasks participating in the cycle are excluded from `ready` results and surfaced via `lithos_task_blocked` with a cycle-error blocker until the cycle is repaired; the cyclic edges are retained so the blocked state remains visible

---

## 9. Priority and Scheduling Policy

This proposal does **not** require a full scheduler service in MVP.

Instead:

- `lithos_task_ready` returns the feasible frontier
- clients sort by existing priority conventions
- a later phase may add canonical ranking rules

Recommended first-class priority field in Phase 4 of this work:

- `priority TEXT CHECK(priority IN ('highest','high','medium','low','lowest'))`

Until then:

- continue reading `metadata.priority`
- surface it unchanged in `ready` and `blocked` views

This keeps the proposal incremental.

---

## 10. Integration with Knowledge and LCMA

This extension should stay tightly coupled to Lithos knowledge features.

### 10.1 Findings

Findings remain the preferred event log for:

- friction
- blockers
- reopen requests
- useful intermediate state

The new task graph complements findings; it does not replace them.

### 10.2 Retrieval

Agents beginning work on a ready task compose their own context from existing surfaces:

- recent findings via existing task/finding reads
- relevant knowledge IDs linked via prior task completion feedback
- optional `lithos_retrieve(..., task_id=...)` context for active tasks

If composing these proves to be a recurring friction point, the deferred `lithos_task_prime` bootstrap tool (§6.1) is the answer — but it should be motivated by observed need, not anticipated need.

### 10.3 Projects

This proposal does not introduce a first-class project table.

Project association should continue using:

- `tags=["project:<slug>"]`
- `metadata.project="<slug>"`

The new task graph should respect those conventions in filters and inherited context.

---

## 11. Phased Delivery

## Phase 1: Task Graph Foundation

- add `task_type`
- add `task_edges` with indexes
- add `lithos_task_edge_upsert`
- add `lithos_task_edge_list`
- add `lithos_task_ready`
- add `lithos_task_blocked`
- one-time backfill of `metadata.depends_on` / `metadata.blocked_on` in the same migration

Exit criteria:

- open tasks with dependency metadata can be queried as ready/blocked without custom client logic

## Phase 2: Hierarchy and Spawn

- accept `epic` on write (already excluded from `ready` via the forward-compatible guard)
- add `parent_task_id` convenience support (creates a `parent_child` edge)
- add `lithos_task_children`
- add `lithos_task_spawn`
- reject `parent_child` cycles on write

Phase 2 is **structural only**: no dedicated roll-up helper (`lithos_task_children` already returns each child's status, so progress is computable) and no epic close-rule enforcement — those roll-up close rules stay in Phase 4 (see §5.2).

Exit criteria:

- agents can decompose and extend work without losing parent/child relationships

## Phase 3: Gates (delivered)

- accept `gate` task type with strict metadata validation (`gate_type`; `timer` requires a normalized `ready_at`)
- accept `waits_on_gate` in `lithos_task_edge_upsert` (rejected before this phase); cycle detection spans the unified `blocks`+`waits_on_gate` dependency graph
- gate-aware readiness via a single `_unsatisfied_blocker_sql` predicate shared by ready/blocked/`_is_task_ready` (completion + query-time `timer`; cancelled = unsatisfiable); `newly_unblocked_by` widened to gate waiters
- **no new MCP tools** — gates are created via `lithos_task_create(task_type='gate')` and resolved via `lithos_task_complete`

Exit criteria:

- waiting states are explicit; agents resolve gates by completing them (or `timer` gates auto-resolve at query time)

## Phase 4: Scheduling Refinements (all optional, motivated by observed need)

- first-class priority column
- deferred edge types (`relates_to`, `duplicate_of`, `superseded_by`, `caused_by`, `validates`) plus their terminal-state rules
- epic roll-up close rules for `parent_child`
- richer ranking/order semantics for ready work
- `lithos_task_prime` bootstrap envelope

---

## 12. Risks

## 12.1 Overlap with `edges.db`

Risk:

- task edges may look like a second graph system

Mitigation:

- keep task edges scoped strictly to coordination
- keep knowledge/provenance/semantic relationships in `edges.db`

## 12.2 Stale Scheduling Metadata

Risk:

- old task rows retain inert `metadata.depends_on` values that an agent might mistake for live scheduling state

Mitigation:

- the scheduler reads only `task_edges`; this is stated in the spec and tool descriptions
- new metadata writes containing `depends_on`/`blocked_on` are rejected (see §7.2), so stale state cannot be recreated through the additive metadata write path
- migration markers on backfilled edges make origin explicit
- (the previously identified dual-source ambiguity risk is eliminated by having no read-compatibility layer at all — see §7)

## 12.3 Scheduler Expectations

Risk:

- users may assume Lithos now provides full autonomous orchestration

Mitigation:

- define MVP as "ready-work computation", not "workflow engine"
- keep ranking and execution policy mostly client-driven at first

## 12.4 Cancelled Blockers Spuriously Readying Dependents

Risk:

- the §5.2 `blocks` semantics, if read as "`to_task_id` cannot be ready *while `from_task_id` is open*", resolve the edge the moment the predecessor leaves `open` — **including when it ends `cancelled`**. A dependent in a strict-sequential chain would then become `ready` even though the work it depends on was abandoned. This silently regresses the pre-extension convention, which required a blocker be `completed` before its dependents run (e.g. a metadata-`depends_on` consumer that gates on `status == "completed"` and fails downstream work when a blocker is cancelled). Any client that switches from that convention to trusting `lithos_task_ready` would start running stories whose predecessor was cancelled — for a decomposed PRD, that means story N+1 building on the abandoned work of story N.

Mitigation (folded into §5.2, §8, §6.1):

- readiness requires every blocking predecessor be `completed`, not merely non-`open`;
- a predecessor that ends `cancelled` leaves its dependents blocked with a `blocker_unsatisfiable` reason (carrying the predecessor id + status) — the same "retain the edge, surface the block, exclude from ready" treatment the backfill applies to cycles;
- Lithos stays passive: it does not auto-cancel or auto-reroute the stranded dependents; the orchestrator decides whether to re-open the blocker, re-route, or cancel the subtree.

This is a semantics clarification, not new machinery: it changes the readiness predicate from "predecessor not open" to "predecessor completed" and adds one blocker kind (`blocker_unsatisfiable`).

---

## 13. Minimal Implementation Checklist

- schema migration for `tasks.task_type`
- schema migration for `task_edges` including `(from_task_id, type)` and `(to_task_id, type)` indexes
- one-time `metadata.depends_on` / `metadata.blocked_on` backfill inside the same migration
- bounded-traversal cycle detection for blocking edges
- coordination service methods for edge CRUD and ready/blocked computation (indexed SQL, no metadata scans)
- MCP tool exposure in `server.py`
- spec updates in `docs/SPECIFICATION.md`
- tests covering:
  - ready queue
  - blocked queue with blocker explanations
  - one-time backfill from `metadata.depends_on` (including dangling IDs)
  - backfill cycles: participating tasks excluded from ready and reported as `cycle` blockers
  - cycle rejection on write
  - cancelled blocker: a dependent of a `cancelled` `blocks` predecessor is excluded from `ready` and reported as a `blocker_unsatisfiable` blocker (not spuriously ready) — §8, §12.4
  - gate blocking, including query-time `timer` resolution (Phase 3)
  - rejection of edge types not yet accepted (deferred types always; `waits_on_gate` before Phase 3)
  - rejection of `metadata.depends_on` / `metadata.blocked_on` on `lithos_task_create` / `lithos_task_update`
  - parent/child listing and non-blocking `parent_child` semantics

---

## 14. Decision

If Lithos is going to be used as an agent issue tracker and scheduler, this is the smallest coherent extension that closes the current platform gaps without changing the core identity of the system.

It keeps Lithos:

- knowledge-first
- local-first
- MCP-native
- convention-friendly

while making task coordination graph-native instead of metadata-interpreted.
