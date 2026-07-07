# Task Graph Reference

Tasks can be linked into a dependency graph: blocking edges control **readiness** (what can be worked on now), hierarchy edges organise work under epics, and gates model waits on the outside world.

> **Two different graphs.** `lithos_task_edge_*` links **tasks** (coordination). `lithos_edge_upsert` / `lithos_edge_list` link **knowledge documents** (typed LCMA edges — see `search-and-retrieval.md`). Don't mix them up.

---

## Edge Types

| Type | Meaning | Blocks readiness? |
|------|---------|-------------------|
| `blocks` | `from` must complete before `to` is ready | Yes |
| `waits_on_gate` | `to` is not ready until gate `from` resolves | Yes |
| `parent_child` | `from` is the parent of `to` (hierarchy) | No |
| `discovered_from` | `to` was discovered while working `from` (provenance) | No |

---

## Declaring Dependencies

At creation (preferred):

```
lithos_task_create(
    title="Deploy service",
    agent="<id>",
    depends_on=["<build-task-id>", "<test-task-id>"],  # each creates a blocks edge
    parent_task_id="<epic-id>"                          # creates a parent_child edge
)
```

After creation:

```
lithos_task_edge_upsert(
    from_task_id="<prerequisite>",
    to_task_id="<dependent>",
    type="blocks",
    agent="<id>"
)
```

Rules:

- **Never put `depends_on` or `blocked_on` in task metadata** — rejected with `invalid_metadata_key`. Dependencies are first-class edges, not metadata
- Referenced tasks must already exist (`task_not_found`); self-edges are rejected (`self_edge`)
- Blocking and hierarchy edges are cycle-checked on write — an edge that would close a cycle is rejected with `cycle`
- A task can have at most one parent (`parent_exists`)
- `waits_on_gate` requires the `from` task to be a gate (`not_a_gate`)

---

## Finding Work: Ready and Blocked

```
lithos_task_ready(project="<slug>", limit=20)
```

**Ready** = open AND not an epic/gate AND every `blocks` predecessor is `completed` AND not held by an unresolved gate.

- **Ready ≠ unclaimed.** Claims are attached to results (`with_claims=True` by default) but never filter them — still `lithos_task_claim` before working
- Filters: `project` (shorthand for `metadata.project`), `tags`, `metadata_match`, `limit`

Diagnose stuck tasks:

```
lithos_task_blocked(project="<slug>")
```

Each blocked task carries a `blockers` list:

| `kind` | Meaning | Action |
|--------|---------|--------|
| `task` | Predecessor still open | Work/complete the predecessor first |
| `gate` | Gate not yet resolved | Resolve the gate (complete the gate task, or wait for a timer) |
| `blocker_unsatisfiable` | Predecessor or gate was **cancelled** — permanently blocked | `lithos_task_reopen` the cancelled blocker, or re-route the edges |
| `cycle` | Dependency chain forms a cycle | Remove an edge to break the cycle |

Ripple effects: `lithos_task_complete` returns `unblocked` (task IDs this completion made ready); `lithos_task_reopen` returns `reblocked` (dependents that lost readiness).

---

## Hierarchy: Epics and Children

- `task_type="epic"` marks a roll-up container. Epics are **excluded from ready** — never claim an epic; work its children
- Attach children with `parent_task_id` on create, or a `parent_child` edge later
- List children:

```
lithos_task_children(task_id="<epic-id>", recursive=False, include_closed=False)
```

`recursive=True` walks the full descendant subtree; `include_closed=True` includes completed/cancelled children.

---

## Spawning Follow-On Work

When work uncovers new work mid-task:

```
lithos_task_spawn(
    source_task_id="<current-task>",
    title="Fix flaky auth test discovered during deploy",
    agent="<id>",
    relation_type="discovered_from"   # or "blocks" if the new task must finish before the source
)
```

- The edge always runs source → spawned; the spawned task is always `task_type="task"`
- Inherits from the source by default: `metadata.project` (`inherit_project`), tags (`inherit_tags`), and the scheduling keys `priority` / `parallelizable` / `phase` (`inherit_context`) — pass `metadata={...}` to override inherited keys

---

## Gates: Waiting on the Outside World

A gate is a task (`task_type="gate"`) representing an external condition:

```
gate = lithos_task_create(
    title="Wait for PR approval",
    agent="<id>",
    task_type="gate",
    metadata={"gate_type": "pr", "repo": "agent-lore/lithos", "pr_number": 123}
)
lithos_task_edge_upsert(
    from_task_id=gate["task_id"], to_task_id="<waiting-task>",
    type="waits_on_gate", agent="<id>"
)
```

- `metadata.gate_type` is **required**: `human | timer | ci | pr | external_task`. Timer gates also require `metadata.ready_at` (ISO timestamp); other keys (`approval_required_from`, `provider`, `run_id`, ...) are advisory
- A gate resolves when its task is **completed** — or automatically for an open `timer` gate whose `ready_at` has passed
- A **cancelled** gate leaves waiters permanently blocked (`blocker_unsatisfiable`)
- Gates are excluded from ready — resolve them, don't work them

---

## Inspecting the Graph

```
lithos_task_edge_list(task_id="...", direction="both", types=["blocks"])
```

- `direction` ∈ `incoming | outgoing | both` (relative to `task_id`)
- Returns edges with `from_task_id`, `to_task_id`, `type`, `direction`, `metadata`, and provenance (`created_by`, `created_at`)
