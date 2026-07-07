# Task Coordination Reference

> Dependencies, hierarchy, spawning, and gates are covered in `task-graph.md`.

## Task Lifecycle

```
open → completed                (lithos_task_complete)
open → cancelled                (lithos_task_cancel)
completed/cancelled → open      (lithos_task_reopen)
```

`lithos_task_reopen(task_id=..., agent=...)` moves a terminal task back to open — it clears `resolved_at` and `outcome`, auto-posts a `[Reopened]` finding, and returns `reblocked` (dependent tasks that lose readiness). Reopening a task that is already open returns `task_not_resolved`.

Complete and cancel require the task to be open (`task_not_found` otherwise). `lithos_task_update` also works on **terminal** tasks — annotate closed tasks without reviving them.

---

## Creating Tasks

```
lithos_task_create(
    title="Implement inbox health check",
    agent="<your-id>",
    description="Add a /health endpoint that reports inbox queue depth",
    tags=["project:influx", "trigger:story-develop"],
    metadata={
        "project": "influx",
        "priority": "medium",
        "scheduled_for": "2026-06-15"
    },
    depends_on=["<prereq-task-id>"],   # optional — creates blocking edges (see task-graph.md)
    parent_task_id="<epic-id>"         # optional — creates a hierarchy edge
)
```

**Required fields**: `title`, `agent`

`task_type` defaults to `"task"`. `"epic"` (roll-up container) and `"gate"` (external wait) are graph concepts — see `task-graph.md`.

### Key Metadata Fields

| Key | Type | Values |
|-----|------|--------|
| `project` | string | Project slug — used by Loom for project association |
| `priority` | string | `highest\|high\|medium\|low\|lowest` |
| `scheduled_for` | string | `YYYY-MM-DD` |
| `parallelizable` | bool | Whether siblings can execute concurrently |
| `phase` | string | Scheduling phase — inherited by `lithos_task_spawn` |

**Forbidden keys**: `depends_on` and `blocked_on` are rejected with `invalid_metadata_key` — dependencies are first-class edges, not metadata. Use the `depends_on` parameter or `lithos_task_edge_upsert` (see `task-graph.md`).

### Tag Conventions

| Tag | Meaning |
|-----|---------|
| `project:<slug>` | Associates task with a project |
| `trigger:<route-name>` | Matches lithos-loom route-runner stanzas |
| `github-issue` | Task originated from a GitHub issue |
| `influx:inbox` | Task is an influx inbox submission |

**Always set both `tags=["project:<slug>"]` and `metadata={"project": "<slug>"}` on tasks.** Loom queries by `metadata.project`; Agent Zero queries by tag. Neither alone gives full coverage.

---

## Updating Tasks

```
lithos_task_update(task_id="...", agent="<id>", title=..., description=..., tags=[...], metadata={...})
```

- At least one of `title` / `description` / `tags` / `metadata` is required
- Works on **terminal** tasks — annotate completed/cancelled tasks without reviving them
- `metadata` is an additive per-key merge: a value overwrites that key, `null` deletes it, unmentioned keys are preserved, `{}` is a no-op
- `tags` replaces the whole list
- Same forbidden keys as create: `depends_on` / `blocked_on` → `invalid_metadata_key`

---

## Claiming Tasks

```
lithos_task_claim(task_id="...", aspect="research", agent="<id>", ttl_minutes=60)
```

- Claims expire after `ttl_minutes` (default 60, max 480)
- Another agent can take an expired claim — that's by design
- Renew before expiry: `lithos_task_renew(task_id="...", aspect="...", agent="<id>", ttl_minutes=60)`
- Release when done or aborting: `lithos_task_release(task_id="...", aspect="...", agent="<id>")`
- `lithos_task_complete` and `lithos_task_cancel` auto-release claims
- **Claim denied**: returns `{status: "error", code: "claim_failed"}` — normal contention (another agent holds the aspect, or the task is closed/missing), not a fault. Try another task or wait
- **Renew/release with no matching claim**: returns `{status: "error", code: "claim_not_found"}` — the claim expired or isn't yours

---

## Posting Findings

Post intermediate results other agents may need before the task completes:

```
lithos_finding_post(
    task_id="...",
    agent="<id>",
    summary="[Friction] Rate limiter config missing from example",
    knowledge_id="<uuid>"   # optional — link to a knowledge doc
)
```

Read a task's findings back with `lithos_finding_list(task_id="...", since="<ISO>")`.

### Conventional Finding Prefixes

| Prefix | Meaning |
|--------|---------|
| `[Friction]` | Operational difficulty needing attention |
| `[ReopenRequested]` | Request that the task owner reopen — use `lithos_task_reopen` directly when the decision is yours |
| `[Reopened]` | Auto-posted by `lithos_task_reopen` |
| `[BlockerFailed]` | Plugin execution failed |

---

## Completing Tasks with Feedback

```
lithos_task_complete(
    task_id="...",
    agent="<id>",
    outcome="Implemented and tested inbox health endpoint",
    cited_nodes=["uuid1", "uuid2"],      # docs that were useful
    misleading_nodes=["uuid3"]           # docs that were misleading
)
```

Returns `{success: true, unblocked: [...]}` — `unblocked` lists tasks this completion made ready (see `task-graph.md`).

### How Feedback Works

- `cited_nodes`: salience +0.02, creates `related_to` edges between cited pairs
- `misleading_nodes`: salience -0.05, weakens all incident edges
- **≥3 misleading marks → auto-quarantined** — excluded from all future search/retrieve
- Nodes neither cited nor misleading get a mild -0.01 "ignored" penalty
- Only nodes that appeared in a `lithos_retrieve` receipt for that task can be cited/misleading

**Always provide feedback when you can** — it's how the knowledge base improves over time.

---

## Looking Up Tasks

- `lithos_task_get(task_id="...")` — returns task directly; use when you know the ID and don't need claims
- `lithos_task_status(task_id="...")` — returns task with its active claims
- `lithos_task_list(status="open", with_claims=True)` — list with claim info; avoids N+1 queries. Filter by `task_type` (`task|epic|gate`), `tags`, `metadata_match`, `resolved_since`
- `lithos_task_ready()` / `lithos_task_blocked()` — the workable frontier, and why the rest is stuck (see `task-graph.md`)
- `lithos_task_children(task_id="...", recursive=True)` — subtasks via hierarchy edges
- `lithos_task_edge_list(task_id="...")` — dependency/hierarchy edges on a task

---

## Tool Responses

- **Success**: `{success: true}` or status envelope with created ID
- **Claim denied**: `{status: "error", code: "claim_failed"}` — normal contention, not a fault
- **Release/renew no match**: `{status: "error", code: "claim_not_found"}` — claim expired or not owned
- **Task not found / already closed**: `{status: "error", code: "task_not_found"}`
- **Reopen on an open task**: `{status: "error", code: "task_not_resolved"}`
