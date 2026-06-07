---
name: lithos
description: |
  Use when working with Lithos via MCP to search or write knowledge, create or coordinate tasks, claim work before starting, post findings, or collaborate with other agents. Covers the full agent workflow: register, search-before-work, knowledge read/write, task lifecycle, project conventions, and tagging.
---

# Lithos

Lithos is a shared knowledge base and task coordination system for multi-agent workflows. Agents interact with it exclusively through MCP tools prefixed `lithos_`. Think of it as "Obsidian for agents" — structured markdown with full-text and semantic search, task claiming, and provenance tracking.

## Trigger Conditions

Load this skill when you need to:
- Search for existing knowledge before starting work
- Read or write knowledge documents
- Create, claim, complete, or cancel tasks
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
Check before starting a task — someone may already be working on it.

## Core Workflows

### Research and Write
1. `lithos_cache_lookup(source_url="https://...")` — check if knowledge already exists
2. `lithos_search(query="<topic>")` or `lithos_retrieve(query="<topic>")` — find related docs
3. Do your research
4. `lithos_write(...)` — store findings with appropriate `note_type` and tags
5. `lithos_edge_upsert(...)` — link to source docs if applicable

### Task Execution
1. `lithos_task_list(status="open", with_claims=True)` — find unclaimed tasks
2. `lithos_task_claim(task_id=..., aspect="<aspect>", agent="<id>", ttl_minutes=60)` — claim before starting
3. `lithos_retrieve(query="<topic>", task_id=...)` — retrieve relevant knowledge
4. Do the work, posting interim results with `lithos_finding_post(...)`
5. `lithos_task_complete(task_id=..., agent="<id>", outcome="...", cited_nodes=[...])` — complete with feedback

### Create a New Task
```
lithos_task_create(
    title="...",
    agent="<your-id>",
    description="...",
    tags=["project:<slug>"],
    metadata={"project": "<slug>", "priority": "medium"}
)
```
Always set **both** `tags=["project:<slug>"]` and `metadata={"project": "<slug>"}` — different agents query by each convention.

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

## Project Conventions

Lithos has no first-class project entity. Projects are represented through conventions:

- **Project context doc**: `projects/<slug>/<slug>-project-context.md`, tagged `["project-context", "project:<slug>"]`, `note_type="concept"`
- **Tag format**: `project:<slug>` (colon separator, e.g. `project:influx`)
- **Metadata**: always also set `metadata={"project": "<slug>"}` on tasks

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
- **Terminal states are final** — no reopen primitive. Post `[ReopenRequested]` finding if a task needs revisiting
- **Always provide feedback on complete** — `cited_nodes` and `misleading_nodes` improve the KB over time
- **Claim denied = `{success: false}`** — not an error; another agent holds it. Try another task or wait

## Pitfalls

- **Don't skip the search step** — the most common mistake. Always `lithos_cache_lookup` or `lithos_search` before writing new knowledge
- **Don't use `lithos_search` when quality matters** — it doesn't enforce access scopes or track salience. Use `lithos_retrieve` for actual work
- **Don't set only tags OR only metadata on tasks** — set both. Loom queries by `metadata.project`; Agent Zero queries by tag
- **`metadata` reserved keys** — don't use: `id, title, author, created_at, updated_at, tags, confidence, source_url, version, status`. They'll return `invalid_input`
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
- `references/project-conventions.md` — project context docs, slug format, idea lifecycle, tagging
- `references/error-handling.md` — all error codes, collision types, retry patterns
