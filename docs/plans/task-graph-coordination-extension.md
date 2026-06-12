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
- `subtask` — child task under a parent
- `gate` — external wait condition

(`message`, an async coordination artifact, was considered and deferred until a concrete use case exists.)

Storage:

- new `task_type TEXT NOT NULL DEFAULT 'task'` column on `tasks`, added by schema migration
- the column is the only source of truth; `metadata.task_type` is not read

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
  Meaning: `to_task_id` cannot be considered ready while `from_task_id` is open. Blocking.
- `parent_child`
  Meaning: `from_task_id` is the parent; `to_task_id` is the child. Purely structural — it never blocks the child. Epic roll-up rules (e.g. a parent cannot close while children are open) operate in the reverse direction and are deferred to Phase 4.
- `discovered_from`
  Meaning: `to_task_id` was discovered during execution of `from_task_id`. Non-blocking; exists to support `lithos_task_spawn` provenance.

Accepted from Phase 3 (alongside gate readiness semantics):

- `waits_on_gate`
  Meaning: `to_task_id` is blocked by the gate task `from_task_id` (same direction as `blocks`: `from_task_id` is the blocker). `to_task_id` cannot be ready until the gate is resolved — i.e. the gate task is closed, or for `timer` gates, `ready_at <= now` at query time. Blocking.

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

- `timer` gates are evaluated at query time: the gate is considered resolved when `ready_at <= now`. No state change is required.
- All other gate types (`human`, `ci`, `pr`, `external_task`) are resolved by an agent (or a hook/script acting as one) completing the gate task via `lithos_task_complete` when it observes the external condition is met.

The gate metadata (`provider`, `run_id`, `pr_number`, etc.) exists so that the resolving agent knows what to check — it does not imply Lithos watches those systems. Anything that polls CI or PR state lives outside Lithos.

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

Return open tasks with no active blocking edges and no unresolved gates.

Arguments:

- `project: str | None = None`
- `tags: list[str] | None = None`
- `metadata_match: dict | None = None`
- `limit: int = 50`
- `include_claims: bool = True`

Returns:

- `{ tasks: [...] }`

Behavior:

- only `status='open'`
- excludes tasks blocked by open blocking predecessors
- excludes tasks blocked by unresolved gates
- optionally excludes already-claimed tasks unless requested otherwise

### `lithos_task_blocked`

Return open tasks that are not ready, with structured blocker reasons.

Arguments:

- same filter surface as `lithos_task_ready`

Returns:

- `{ tasks: [{..., blockers: [...]}] }`

Blocker entries should include:

- `kind`: `task | gate | cycle`
- `task_id` or gate reference
- `type`
- `message`

There is no `external` blocker kind: external waits are always represented as gate tasks (§5.3), so every blocker is either a task, a gate, or a cycle error from the backfill (§8). `cycle` blockers carry the task IDs forming the cycle in `message`.

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

- can inherit `metadata.project`, project tag, and selected scheduling metadata
- creates the relation edge automatically, always with the source task as `from_task_id` and the spawned task as `to_task_id`:
  - `discovered_from`: source → spawned (the spawned task was discovered during the source task)
  - `blocks`: source → spawned (the spawned task is blocked until the source task closes). Spawning a task that blocks its source is not supported; use `lithos_task_edge_upsert` explicitly for that.

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
2. the task itself is not a `gate`
3. it has no unresolved incoming blocking edges
4. it is not blocked by an unresolved gate
5. if filtering excludes claimed tasks, it has no active conflicting claims

MVP blocked rule:

A task is blocked when:

- it is open
- and at least one unresolved blocking predecessor or gate applies

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

- add `parent_task_id` convenience support
- add `lithos_task_children`
- add `lithos_task_spawn`
- add epic roll-up helpers

Exit criteria:

- agents can decompose and extend work without losing parent/child relationships

## Phase 3: Gates

- add `gate` task type semantics
- add `waits_on_gate` to the accepted edge types in `lithos_task_edge_upsert` (it is rejected before this phase)
- add unresolved-gate support in readiness (`waits_on_gate` edges, query-time `timer` evaluation)

Exit criteria:

- waiting states are explicit, and agents can resolve gates by completing gate tasks

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
