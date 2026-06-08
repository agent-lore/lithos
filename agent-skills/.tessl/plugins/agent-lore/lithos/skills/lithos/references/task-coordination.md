# Task Coordination Reference

## Task Lifecycle

```
open → completed   (lithos_task_complete)
open → cancelled   (lithos_task_cancel)
```

Terminal states are final. No reopen primitive. If a task needs revisiting, post a `[ReopenRequested]` finding.

Only open tasks can be updated, completed, or cancelled — operations on closed tasks return errors.

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
    }
)
```

**Required fields**: `title`, `agent`

### Key Metadata Fields

| Key | Type | Values |
|-----|------|--------|
| `project` | string | Project slug — used by Loom for project association |
| `priority` | string | `highest\|high\|medium\|low\|lowest` |
| `scheduled_for` | string | `YYYY-MM-DD` |
| `depends_on` | list[str] | Prerequisite task IDs |
| `parallelizable` | bool | Whether siblings can execute concurrently |

### Tag Conventions

| Tag | Meaning |
|-----|---------|
| `project:<slug>` | Associates task with a project |
| `trigger:<route-name>` | Matches lithos-loom route-runner stanzas |
| `github-issue` | Task originated from a GitHub issue |
| `influx:inbox` | Task is an influx inbox submission |

**Always set both tag and metadata**: `tags=["project:<slug>"]` + `metadata={"project": "<slug>"}`. Loom queries by `metadata.project`; Agent Zero queries by tag. Setting both ensures discoverability regardless of which agent created the task.

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
- **Claim denied**: returns `{success: false}` — NOT an error envelope. Try another task or wait

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

### Conventional Finding Prefixes

| Prefix | Meaning |
|--------|---------|
| `[Friction]` | Operational difficulty needing attention |
| `[ReopenRequested]` | Task needs revisiting (no reopen primitive) |
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
- `lithos_task_list(status="open", with_claims=True)` — list with claim info; avoids N+1 queries

---

## Tool Responses

- **Success**: `{success: true}` or status envelope with created ID
- **Claim denied**: `{success: false}` — NOT an error envelope
- **Release/renew no match**: `{success: false}` — claim expired or not owned
- **Task not found / already closed**: `{status: "error", code: "task_not_found"}`
