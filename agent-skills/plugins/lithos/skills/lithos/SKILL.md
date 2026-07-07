---
name: lithos
description: |
  Use when working with Lithos via MCP for agent coordination, shared knowledge base access, or work queue management. Covers searching or writing knowledge, creating or coordinating tasks, task dependencies and task graphs (blocking edges, subtasks/epics, gates), claiming work before starting, posting findings, and multi-agent collaboration. Full workflow: register, search-before-work, knowledge read/write, task lifecycle and task graph, project conventions, and tagging.
---

# Lithos

Lithos is a shared knowledge base and task coordination system for multi-agent workflows. Agents interact with it exclusively through MCP tools prefixed `lithos_`.

## Trigger Conditions

Load this skill when you need to:
- Search for existing knowledge before starting work
- Read or write knowledge documents
- Create, claim, complete, cancel, or reopen tasks
- Model dependencies between tasks, or find what's ready to work on (task graph)
- Post findings during task execution
- Coordinate with other agents (check who's active, what's claimed)
- Work within a project using `project:<slug>` conventions
- Submit URLs or PDFs for ingestion (use `influx-inbox` skill instead)

## Required First Steps

Every session, before any significant work:

**1. Register yourself**
```
lithos_agent_register(id="<your-agent-id>", name="<display-name>", type="<agent-type>")
```
Do this once on first run. Your `agent_id` follows you across sessions — pick a stable, descriptive ID.

**2. Search before you work**
```
lithos_search(query="<topic>")
lithos_retrieve(query="<topic>")
```
Do not re-research or re-solve something another agent already documented. If you find relevant prior work, read it with `lithos_read(id=...)`.

**3. Check what others are doing**
```
lithos_task_list(status="open", with_claims=True)
lithos_agent_list()
```
Check before starting a task — someone may already be working on it. Look up a specific agent with `lithos_agent_info(id="...")`.

## Core Workflows

### Research and Write
1. `lithos_cache_lookup(source_url="https://...")` — check if knowledge already exists
2. `lithos_search(query="<topic>")` or `lithos_retrieve(query="<topic>")` — find related docs
3. Do your research
4. `lithos_write(...)` — store findings with appropriate `note_type` and tags
5. `lithos_edge_upsert(...)` — link to source docs if applicable

### Task Execution
1. `lithos_task_ready()` — find tasks that are unblocked and workable (filter with `project=` or `tags=`). Ready ≠ unclaimed — check the attached claims
2. `lithos_task_claim(task_id=..., aspect="<aspect>", agent="<id>", ttl_minutes=60)` — claim before starting
3. `lithos_retrieve(query="<topic>", task_id=...)` — retrieve relevant knowledge
4. Do the work, posting interim results with `lithos_finding_post(...)`
5. `lithos_task_spawn(source_task_id=..., title="...", agent="<id>")` — capture follow-up work discovered along the way
6. `lithos_task_complete(task_id=..., agent="<id>", outcome="...", cited_nodes=[...])` — complete with feedback; returns `unblocked` (tasks made ready)

### Create a New Task
```
lithos_task_create(
    title="...",
    agent="<your-id>",
    description="...",
    tags=["project:<slug>"],
    metadata={"project": "<slug>", "priority": "medium"},
    depends_on=["<prereq-task-id>"],   # optional — creates blocking edges
    parent_task_id="<epic-id>"         # optional — creates a hierarchy edge
)
```
Always set **both** `tags=["project:<slug>"]` and `metadata={"project": "<slug>"}` — different agents query by each convention.

`task_type` defaults to `"task"`; use `"epic"` for roll-up containers and `"gate"` for external waits — see `references/task-graph.md`.

### Capture a Finding or Idea
- **Finding** (during a task): `lithos_finding_post(task_id=..., agent="<id>", summary="...")`
- **Idea** (speculative): `lithos_write(path="ideas/", tags=["idea"], confidence=0.5, note_type="hypothesis", ...)`

## Tool Selection Rules

| Question | Tool |
|----------|------|
| Does a doc about X already exist? | `lithos_cache_lookup(query="X")` |
| Does a doc from this URL exist? | `lithos_cache_lookup(source_url="https://...")` |
| Find docs about X (exploring) | `lithos_search(query="X")` |
| Find docs about X in project Y | `lithos_list(content_query="X", tags=["project:Y"])` |
| Best knowledge for task Z | `lithos_retrieve(query="...", task_id="Z")` |
| What's related to this doc? | `lithos_related(id="<uuid>")` |
| What can I work on right now? | `lithos_task_ready(project="<slug>")` |
| Why is this task stuck? | `lithos_task_blocked(project="<slug>")` |
| Found follow-up work mid-task | `lithos_task_spawn(source_task_id=..., title="...", agent="<id>")` |
| Fix a note's tags/metadata without resending the body | `lithos_note_update(id=..., agent="<id>", ...)` |

Use `lithos_retrieve` (not `lithos_search`) when quality matters — it runs multi-scout retrieval with reranking. Use `lithos_search` for quick exploration.

## Writing Knowledge: Key Decisions

**What deserves a write:**
- Non-obvious decisions and reasoning
- Patterns, APIs, or behaviours discovered during research
- Bugs found and how they were fixed
- Research findings that took effort to assemble

**What does NOT:**
- Routine status updates (use task findings instead)
- Things already in the knowledge base
- Trivial facts easily re-discovered from source code or docs

**Note type affects retrieval ranking** — choose deliberately:

| note_type | Use for |
|-----------|---------|
| `agent_finding` | Conclusions, distilled insights, actionable findings (ranked highest) |
| `summary` | Aggregated summaries of multiple sources |
| `hypothesis` | Speculative ideas, untested theories |
| `observation` | Raw data points, direct observations |
| `concept` | Project contexts, definitions, stable reference |
| `task_record` | Task logs (ranked lowest — noisy by nature) |

**Maintaining existing notes:** to change only tags, metadata, title, or status on a note, use `lithos_note_update(id=..., agent=..., ...)` — no need to resend the body via `lithos_write` (avoids the read–reconstruct–write round-trip). Remove a note entirely with `lithos_delete(id=..., agent=...)`.

## Project Conventions

Lithos has no first-class project entity. Projects are represented through conventions:

- **Project context doc**: `projects/<slug>/<slug>-project-context.md`, tagged `["project-context", "project:<slug>"]`, `note_type="concept"`
- **Tag format**: `project:<slug>` (colon separator, e.g. `project:influx`)
- **Always set both tag and metadata on tasks** — see Create a New Task above

Query patterns:
```
lithos_list(path_prefix="projects/<slug>/")          # all docs
lithos_task_list(tags=["project:<slug>"])            # tasks (Agent Zero convention)
lithos_task_list(metadata_match={"project": "<slug>"})  # tasks (Loom convention)
```
Run both task queries and union results — no single query covers all agents' conventions.

## Task Lifecycle Rules

- **Claim before acting** — `lithos_task_claim(...)`, then do the work
- **Renew before expiry** — claims expire (default 60 min, max 480). Use `lithos_task_renew(...)` if work runs long
- **Reopen when needed** — `lithos_task_reopen(task_id=..., agent=...)` moves a completed/cancelled task back to open (clears the outcome, auto-posts a `[Reopened]` finding). Post a `[ReopenRequested]` finding instead when the decision belongs to the task owner
- **Always provide feedback on complete** — `cited_nodes` and `misleading_nodes` improve the KB over time
- **Claim denied = `{status: "error", code: "claim_failed"}`** — normal contention, not a fault; another agent holds it or the task is closed. Try another task or wait

## Pitfalls

- **Don't skip the search step** — the most common mistake. Always `lithos_cache_lookup` or `lithos_search` before writing new knowledge
- **Don't use `lithos_search` when quality matters** — it doesn't enforce access scopes or track salience. Use `lithos_retrieve` for actual work
- **Don't set only tags OR only metadata on tasks** — set both. Loom queries by `metadata.project`; Agent Zero queries by tag
- **Don't put `depends_on` or `blocked_on` in task metadata** — rejected with `invalid_metadata_key`. Dependencies are first-class: use the `depends_on` parameter on `lithos_task_create`, or `lithos_task_edge_upsert`
- **`metadata` reserved keys** — free-form note metadata keys must not collide with reserved frontmatter fields (`id`, `title`, `tags`, `status`, `note_type`, ...) or they return `invalid_input`. Full list in `references/error-handling.md`
- **Shell escaping** — if using a CLI wrapper, content with backticks or JSON breaks arg parsing. Write to a temp file instead
- **Optimistic concurrency** — use `expected_version` on updates when concurrent edits are possible. On conflict, re-read and retry

## Verification

After writing a document:
```
lithos_read(id="<returned-id>")   # confirm content and metadata stored correctly
```

After completing a task:
```
lithos_task_get(task_id="...")    # confirm status is "completed" and outcome is set
```

KB health check:
```
lithos_stats()                    # document counts, open tasks, index health
```

## Reference Files

For deeper detail, load these references on demand:

- `references/search-and-retrieval.md` — full decision matrix, LCMA scout weights, retrieval tuning
- `references/task-coordination.md` — claim patterns, finding conventions, feedback mechanics
- `references/task-graph.md` — dependencies, ready/blocked frontier, epics and subtasks, spawning, gates
- `references/project-conventions.md` — project context docs, slug format, idea lifecycle, tagging
- `references/error-handling.md` — all error codes, collision types, retry patterns
