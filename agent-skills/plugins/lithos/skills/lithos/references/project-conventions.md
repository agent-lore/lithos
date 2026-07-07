# Project Conventions Reference

## Projects in Lithos

Lithos has no first-class project entity. Projects are represented through two conventions applied together: a **project context document** and a **tag + metadata pair** on all related tasks and docs.

---

## Project Context Documents

| Field | Value |
|-------|-------|
| Path | `projects/<slug>/<slug>-project-context.md` |
| Tags | `["project-context", "project:<slug>"]` |
| note_type | `concept` |
| Slug format | `^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$` |

Create a project context doc when starting a new project:
```
lithos_write(
    title="<Project Name> Project Context",
    content="...",
    agent="<id>",
    path="projects/<slug>/<slug>-project-context.md",
    tags=["project-context", "project:<slug>"],
    note_type="concept"
)
```

---

## Tag and Metadata Conventions

All tasks and documents belonging to a project should carry **both**:

- **Tag**: `project:<slug>` (colon separator — e.g. `project:lithos-core`, `project:influx`)
- **Metadata**: `{"project": "<slug>"}` on tasks

**Why both?** Different agents use different conventions:
- **Agent Zero** queries by `tags: ["project:<slug>"]`
- **Loom** stores project in `metadata.project` only, with no project tag
- Setting both ensures discoverability regardless of which agent created the item

---

## Project Query Patterns

| Goal | Tool Call |
|------|-----------|
| All docs for a project | `lithos_list(path_prefix="projects/<slug>/")` |
| All tasks (Agent Zero) | `lithos_task_list(tags=["project:<slug>"])` |
| All tasks (Loom) | `lithos_task_list(metadata_match={"project": "<slug>"})` |
| Ready (workable) tasks for a project | `lithos_task_ready(project="<slug>")` — matches `metadata.project`; run `tags=["project:<slug>"]` too (union caveat below) |
| Blocked tasks and why | `lithos_task_blocked(project="<slug>")` |
| Search within a project | `lithos_search(query="...", path_prefix="projects/<slug>/")` |
| All project contexts | `lithos_list(path_prefix="projects/", tags=["project-context"])` |
| Recently closed tasks | `lithos_task_list(resolved_since="2026-06-01T00:00:00Z")` |

> There is no single query that returns all tasks for a project regardless of which agent created them. Run both `tags` and `metadata_match` queries and union the results.

---

## Ideas (Pre-Project Knowledge)

Ideas live separately from project knowledge until they mature:

| Field | Value |
|-------|-------|
| Path | `ideas/` |
| Tags | `["idea"]` (add `"project:<slug>"` if associated) |
| Confidence | 0.5–0.7 (signals speculative/unvalidated) |
| note_type | `hypothesis` or `concept` |

### Idea Lifecycle

- **Becomes a task** → create task tagged with project, update idea doc to link
- **Becomes project knowledge** → move to `projects/<slug>/`, update tags
- **Spawns a new project** → create project context document, tag idea

### Idea Query Patterns

| Goal | Tool Call |
|------|-----------|
| All unattached ideas | `lithos_list(tags=["idea"])` |
| Ideas for a project | `lithos_list(tags=["idea", "project:<slug>"])` |
| Search ideas semantically | `lithos_search(query="...", path_prefix="ideas/")` |

---

## Tagging Conventions

Tags are the primary discovery mechanism. Be generous. Use `lithos_tags(prefix="...")` to discover existing tags before inventing new ones.

| Pattern | Purpose |
|---------|---------|
| `project:<slug>` | Project association — always use colon, not slash |
| `agent-memory`, `lithos`, `architecture`, `mcp` | Topic tags |
| `research`, `decision`, `bug`, `guide` | Type tags |
| `project-context`, `idea`, `influx:inbox` | Functional tags |

---

## Namespaces

Path determines namespace for access scoping and retrieval filtering:

| Path | Namespace |
|------|-----------|
| `projects/<slug>/foo.md` | `projects/<slug>` |
| `research/foo.md` | `research` |
| `ideas/foo.md` | `ideas` |
| Root-level | `default` |

Use `namespace_filter` on `lithos_retrieve` for project-scoped cognitive retrieval.
