# Task Graph Coordination Extension

Status: Proposal

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
4. **Backward-compatible migration path** from today's task metadata conventions.
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
- `message` — optional future async coordination artifact

Storage options:

- Preferred: new nullable `task_type TEXT NOT NULL DEFAULT 'task'` column on `tasks`
- Transitional compatibility: continue accepting `metadata.task_type` on reads until migration is complete

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
```

Semantics:

- `from_task_id` is the source task
- `to_task_id` is the target task
- `type` determines scheduling semantics

Initial edge types:

- `blocks`
  Meaning: `to_task_id` cannot be considered ready while `from_task_id` is open
- `parent_child`
  Meaning: `from_task_id` is the parent; `to_task_id` is the child
- `discovered_from`
  Meaning: `to_task_id` was discovered during execution of `from_task_id`
- `caused_by`
  Meaning: `to_task_id` exists because `from_task_id` is a root cause or precursor
- `validates`
  Meaning: `from_task_id` is a verification task for `to_task_id`
- `relates_to`
  Meaning: informational, non-blocking link
- `duplicate_of`
  Meaning: source task is a duplicate of target task
- `superseded_by`
  Meaning: source task has been replaced by target task
- `waits_on_gate`
  Meaning: a task is blocked by a gate task

Blocking edge types in MVP:

- `blocks`
- `parent_child` only when parent is itself blocked or explicitly configured to block children
- `waits_on_gate`

Non-blocking edge types in MVP:

- `discovered_from`
- `caused_by`
- `validates`
- `relates_to`
- `duplicate_of`
- `superseded_by`

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
- cycles in blocking edges must be rejected
- `duplicate_of` and `superseded_by` must not point to self

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

- `kind`: `task | gate | external`
- `task_id` or gate reference
- `type`
- `message`

### `lithos_task_spawn`

Create a follow-on task linked to an existing source task.

Arguments:

- `source_task_id: str`
- `title: str`
- `agent: str`
- `description: str | None = None`
- `relation_type: "discovered_from" | "caused_by" | "blocks" | "relates_to"`
- `inherit_project: bool = True`
- `inherit_tags: bool = True`
- `inherit_context: bool = True`
- `metadata: dict | None = None`

Returns:

- `{ task_id: str }`

Behavior:

- can inherit `metadata.project`, project tag, and selected scheduling metadata
- creates the relation edge automatically

### `lithos_task_children`

Return child tasks for a parent/epic.

Arguments:

- `task_id: str`
- `recursive: bool = False`
- `include_closed: bool = False`

Returns:

- `{ tasks: [...] }`

### `lithos_task_prime`

Return a compact task handoff/context payload for an agent beginning work.

Arguments:

- `task_id: str`
- `agent_id: str | None = None`

Returns a combined view of:

- task record
- active claims
- parent/child edges
- blockers
- recent findings
- optionally recent cited knowledge or retrieval context

This is the scheduling-side equivalent of a task bootstrap envelope.

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

- if task is marked duplicate or superseded, enforce consistent terminal behavior
- optionally emit newly unblocked tasks in response payload

### `lithos_task_list`

Enhancements:

- optional `task_type`
- optional `ready_only`
- optional `blocked_only`

These should be implemented by delegating to the edge model, not by scanning metadata.

---

## 7. Migration Strategy

## 7.1 Read Compatibility

For a transition period, Lithos should understand today's metadata conventions:

- `metadata.depends_on`
- `metadata.priority`
- `metadata.parallelizable`
- `metadata.blocked_on`

Read behavior during transition:

- `depends_on` can be projected into virtual blocking edges for ready/blocked queries if no canonical edges exist yet
- canonical `task_edges` takes precedence when present

## 7.2 One-Time Backfill

Provide a migration/backfill command or internal admin routine:

1. Scan open tasks
2. Read `metadata.depends_on`
3. Create canonical `blocks` edges
4. Optionally record migration marker in edge metadata:
   - `{"migrated_from": "metadata.depends_on"}`

This lets current projects such as those already using dependency metadata become immediately scheduler-aware.

## 7.3 Metadata Convergence

After backfill:

- `metadata.depends_on` becomes deprecated
- `metadata.priority` may remain supported, but should gain a first-class mirror if scheduling relies on it heavily
- `metadata.parallelizable` can remain metadata in MVP, since it is advisory rather than structural

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

- cycles in blocking edges are rejected on write
- existing cycles found during migration are surfaced as explicit errors and excluded from ready computation until repaired

---

## 9. Priority and Scheduling Policy

This proposal does **not** require a full scheduler service in MVP.

Instead:

- `lithos_task_ready` returns the feasible frontier
- clients sort by existing priority conventions
- a later phase may add canonical ranking rules

Recommended first-class priority field in Phase 2 of this work:

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

`lithos_task_prime` should be able to include:

- recent findings
- relevant knowledge IDs linked via prior task completion feedback
- optional `lithos_retrieve(..., task_id=...)` context for active tasks

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
- add `task_edges`
- add `lithos_task_edge_upsert`
- add `lithos_task_edge_list`
- add `lithos_task_ready`
- add `lithos_task_blocked`
- backfill `metadata.depends_on`

Exit criteria:

- open tasks with dependency metadata can be queried as ready/blocked without custom client logic

## Phase 2: Hierarchy and Spawn

- add `parent_task_id` convenience support
- add `lithos_task_children`
- add `lithos_task_spawn`
- add epic roll-up helpers

Exit criteria:

- agents can decompose and extend work without losing parent/child relationships

## Phase 3: Gates and Bootstrap

- add `gate` task type semantics
- add unresolved-gate support in readiness
- add `lithos_task_prime`

Exit criteria:

- waiting states are explicit and agents can resume from a compact handoff surface

## Phase 4: Scheduling Refinements

- optionally add first-class priority column
- optionally add duplicate/supersession auto-close rules
- optionally add richer ranking/order semantics for ready work

---

## 12. Risks

## 12.1 Overlap with `edges.db`

Risk:

- task edges may look like a second graph system

Mitigation:

- keep task edges scoped strictly to coordination
- keep knowledge/provenance/semantic relationships in `edges.db`

## 12.2 Partial Migration Complexity

Risk:

- mixed old metadata and new canonical edges may create ambiguity

Mitigation:

- canonical `task_edges` always wins
- migration markers make origin explicit
- deprecate but do not immediately reject `metadata.depends_on`

## 12.3 Scheduler Expectations

Risk:

- users may assume Lithos now provides full autonomous orchestration

Mitigation:

- define MVP as "ready-work computation", not "workflow engine"
- keep ranking and execution policy mostly client-driven at first

---

## 13. Minimal Implementation Checklist

- schema migration for `tasks.task_type`
- schema migration for `task_edges`
- cycle detection for blocking edges
- coordination service methods for edge CRUD and ready/blocked computation
- MCP tool exposure in `server.py`
- spec updates in `docs/SPECIFICATION.md`
- tests covering:
  - ready queue
  - blocked queue
  - migration from `metadata.depends_on`
  - cycle rejection
  - gate blocking
  - parent/child listing

---

## 14. Decision

If Lithos is going to be used as an agent issue tracker and scheduler, this is the smallest coherent extension that closes the current platform gaps without changing the core identity of the system.

It keeps Lithos:

- knowledge-first
- local-first
- MCP-native
- convention-friendly

while making task coordination graph-native instead of metadata-interpreted.
