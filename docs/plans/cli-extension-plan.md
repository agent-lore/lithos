# CLI Extension Plan

## Introduction

Lithos is designed primarily as an MCP server — its interface is intended for AI agents, and its existing CLI reflects that: the current commands (`serve`, `reindex`, `validate`, `stats`, `search`) are operator tools for managing and inspecting the server rather than for working with the knowledge base itself. This means that humans and scripts have no straightforward way to read, write, or navigate knowledge documents from the command line without running a full MCP client. Since Lithos already ships a Click-based CLI and the entire domain model (`KnowledgeManager`, `SearchEngine`, `KnowledgeGraph`, `CoordinationService`) is implemented in Python, the natural path is to extend `cli.py` with a user-facing CRUD and graph-exploration layer rather than introduce a separate tool. This change adds that layer incrementally — beginning with machine-readable JSON output across all commands (which unlocks scripting immediately), then document reading and listing, then write operations, and finally graph traversal and coordination commands — making Lithos useful to humans and automation pipelines without duplicating any business logic or adding new dependencies.

## Current State (after #16)

The `inspect` command group added in PR #16 already covers some ground:

| Command | Notes |
|---------|-------|
| `inspect doc <id\|path>` | Read a document's metadata; `--content` for body |
| `inspect agents` | List registered agents |
| `inspect tasks` | List coordination tasks |
| `inspect health` | Backend health check |

These are a good foundation but are read-only and lack machine-readable output. The plan below builds on them.

## Phase 1 — Machine-readable output (cross-cutting, highest leverage)

**Goal:** Make the CLI scriptable before adding new commands.

- Add an `--output [table|json]` option to the root `cli` group so it flows via `ctx.obj` to all subcommands
- Retrofit `search`, `inspect doc`, `inspect agents`, `inspect tasks`, `stats` to honour `--output json`
- Standardise exit codes: `0` = success, `1` = not found / error, `2` = usage error
- Add `--output` to `docs/cli.md`

**Why first:** Every subsequent command gets JSON for free if the pattern is established early.

## Phase 2 — Document reading and listing

**Goal:** Browse the knowledge base without an MCP client.

### `lithos get <path|id>`

A user-friendly evolution of `inspect doc`:

- `--output json|yaml|md|plain` (`plain` = body only, `md` = full file with frontmatter, `json` = structured)
- Defaults to a readable summary (like `inspect doc`) when no `--output` given
- Accepts both UUID and file path as identifier (already done in `inspect doc` — can reuse or alias)

### `lithos list`

Lists all documents, one per line (path + title).

- Filter options: `--tag TAG`, `--author AGENT`, `--since DATE`
- `--output json|table`
- `--limit N` (default: 50)
- Backed by `KnowledgeManager.list_all()`

### `lithos tags`

Lists all tags with document counts, sorted by count descending.

- `--output json|table`
- Backed by `KnowledgeManager.get_all_tags()`

## Phase 3 — Document writing (CRUD)

**Goal:** Create and modify knowledge documents from the command line or scripts.

### `lithos create <path>`

- Body from `--file FILE` or stdin (if no `--file` and stdin is a pipe)
- Frontmatter options: `--title TEXT`, `--tags TAG,...`, `--author AGENT`
- Prints the new document's UUID on success
- Backed by `KnowledgeManager.create()`

### `lithos update <path|id>`

- Patch frontmatter: `--title TEXT`, `--add-tag TAG`, `--remove-tag TAG`
- Replace body: `--body-file FILE` or stdin
- Prints updated document path on success
- Backed by `KnowledgeManager.update()`

### `lithos delete <path|id>`

- Prompts for confirmation unless `--force`
- Exits `0` on success, `1` if not found
- Backed by `KnowledgeManager.delete()`

## Phase 4 — Graph exploration

**Goal:** Navigate the knowledge graph from the command line.

### `lithos graph neighbors <id>`

- Lists documents that link to or from the given document
- `--direction [in|out|both]` (default: `both`)
- `--output json|table`
- Backed by `KnowledgeGraph`

### `lithos graph path <id> <id>`

- Finds the shortest relationship path between two documents
- Prints the chain of titles/paths
- `--output json|table`
- Backed by NetworkX shortest-path on `KnowledgeGraph`

### `lithos graph orphans`

- Lists documents with no incoming or outgoing links
- Useful for knowledge base hygiene
- `--output json|table`

## Phase 5 — Coordination write operations

**Goal:** Allow agents and scripts to participate in task coordination from the CLI.

(`inspect agents` and `inspect tasks` already cover the read side.)

### `lithos tasks claim <id>`

- Claims a task on behalf of `--agent AGENT` for `--aspect ASPECT`
- `--ttl MINUTES` (default: 30)
- Backed by `CoordinationService.claim_task()`

### `lithos tasks complete <id>`

- Marks a task complete with optional `--finding TEXT`
- Backed by `CoordinationService.complete_task()`

### `lithos tasks create`

- Creates a new coordination task
- `--title TEXT`, `--description TEXT`, `--tags TAG,...`

## Phase 6 — Shell completion & polish

**Goal:** Make the CLI pleasant for daily use.

- Enable Click's built-in shell completion (`lithos --install-completion`)
- Path arguments complete against the knowledge directory
- Add `--quiet` / `-q` flag to suppress progress output (useful in scripts)
- Ensure all commands print nothing to stdout on success when `--output json` (errors go to stderr)
- Update `docs/cli.md` with all new commands

## Suggested Delivery Order

```
Phase 1  →  Phase 2  →  Phase 3  →  Phase 4  →  Phase 5  →  Phase 6
  (json)     (read)      (write)     (graph)     (coord)     (polish)
```

Phases 1–3 deliver the most immediate value for both humans and scripts. Phases 4–5 are useful but can wait until the CRUD layer is stable. Phase 6 is ongoing polish.

## Notes on Implementation

- All new commands follow the same pattern as existing ones: lazy imports inside the command function, `asyncio.run()` wrapping async calls
- A small `_output()` helper in `cli.py` that switches between `click.echo(tabulate(...))` and `click.echo(json.dumps(...))` based on `ctx.obj["output"]` will keep the boilerplate minimal
- No new dependencies needed — `json` is stdlib, Click is already present
