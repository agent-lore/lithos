# CLI Admin / Client Split

## Context

The current `lithos` CLI imports `KnowledgeManager`, `SearchEngine`, `KnowledgeGraph`, and `CoordinationService` directly and operates on the local data directory in-process. Every subcommand other than `serve` opens Tantivy, ChromaDB, and the SQLite coordination DB itself. Two consequences follow:

1. A user running an everyday command (`lithos search`, `lithos stats`, `lithos inspect doc`) while a `lithos serve` instance is running contends with the server on the same SQLite/Chroma files.
2. CLI invocations bypass the audit log, telemetry, and access-control paths that the MCP tools record server-side, so human and agent activity diverge in observability.

User-level commands should go through MCP so there is one writer/reader of the data dir at a time, and so audit / observability behave the same regardless of caller. Admin commands (server lifecycle, index rebuilds, drift reconciliation, audit-log reads, backend health probes) genuinely need direct on-disk access and should stay local — but they should be quarantined under their own group so the boundary is obvious.

This plan is a **single binary with two top-level groups**, not two separate binaries. Packaging stays unchanged. We can promote to two binaries later if the client surface grows enough to justify it.

This supersedes `cli-extension-plan.md`. That earlier plan proposed a flat user-facing CRUD/graph/coordination CLI backed by direct `KnowledgeManager` / `CoordinationService` calls — the split below replaces those direct calls with MCP tool calls. The cross-cutting concerns from the old Phase 1 (JSON output) and Phase 6 (shell completion / polish) are folded into this plan as section "Cross-cutting".

## Recommendation

Reorganise `src/lithos/cli.py` into two top-level groups under the existing `cli` entrypoint.

### `lithos admin …` — direct local access (current behaviour)

| Command | Today | Notes |
|---|---|---|
| `admin serve` | `lithos serve` | unchanged; only command that *starts* the server |
| `admin reindex` | `lithos reindex` | rebuilds Tantivy + Chroma; must be local |
| `admin reconcile` | `lithos reconcile` | repairs drift; must be local |
| `admin validate` | `lithos validate` | walks markdown corpus; must be local |
| `admin audit` | `lithos audit` | reads coordination audit table; must be local (no MCP tool exposes this) |
| `admin inspect health` | `lithos inspect health` | probes Tantivy/Chroma backends directly |

Admin commands keep importing `KnowledgeManager` etc. as today. They should refuse (or warn loudly) when a server lockfile/PID is detected — out of scope for this plan but worth noting as a follow-up.

### `lithos client …` — talks to a running server over MCP

| Command | Backed by MCP tool |
|---|---|
| `client write` | `lithos_write` |
| `client read` | `lithos_read` |
| `client delete` | `lithos_delete` |
| `client search` | `lithos_search` |
| `client retrieve` | `lithos_retrieve` |
| `client list` | `lithos_list` |
| `client tags` | `lithos_tags` |
| `client related` | `lithos_related` |
| `client stats` | `lithos_stats` |
| `client inspect doc` | `lithos_read` (formatted) |
| `client inspect agents` | `lithos_agent_list` |
| `client inspect tasks` | `lithos_task_list` |
| `client task {create,claim,renew,release,complete,cancel,update,status,list}` | `lithos_task_*` |
| `client agent {register,info,list}` | `lithos_agent_*` |
| `client finding {post,list}` | `lithos_finding_*` |
| `client edge {upsert,list}` | `lithos_edge_*` |
| `client cache lookup` | `lithos_cache_lookup` |
| `client conflict resolve` | `lithos_conflict_resolve` |
| `client node-stats` | `lithos_node_stats` |

Client commands take `--mcp-url` (default `LITHOS_MCP_URL`, falling back to `http://localhost:8765/sse`), open an MCP SSE client, call the tool, render the response. They never import `KnowledgeManager`, `SearchEngine`, `KnowledgeGraph`, or `CoordinationService`.

### Cutover

- Hard cutover. The existing top-level commands (`serve`, `reindex`, `validate`, `stats`, `search`, `reconcile`, `audit`, `inspect …`) are removed; their behaviour is reachable only via the new `admin` / `client` paths. No aliases, no deprecation period.
- 1:1 read-only equivalents (`stats` ↔ `lithos_stats`, `search` ↔ `lithos_search`, `inspect doc` ↔ `lithos_read`, `inspect agents` ↔ `lithos_agent_list`, `inspect tasks` ↔ `lithos_task_list`) move to `client`.
- Commands with no MCP equivalent today (`audit`, `inspect health`) stay admin-only.

## Cross-cutting (applies to both groups)

These were Phase 1 and Phase 6 of the previous plan; they remain valid and should be implemented alongside the split rather than as a separate stream.

### Machine-readable output

- Add `--output [table|json]` to the root `cli` group so it flows via `ctx.obj` to all subcommands in both groups.
- Standardised exit codes: `0` = success, `1` = not found / error, `2` = usage error.
- All commands print nothing to stdout on success when `--output json` is set if there is no payload; errors go to stderr.
- A small `_output()` helper (in `cli/_format.py`) switches between `click.echo(tabulate(...))` and `click.echo(json.dumps(...))` based on `ctx.obj["output"]` to keep boilerplate minimal.
- For `client` commands the JSON form should pass the MCP envelope through unchanged where possible, so scripts get the same shape they would get from the MCP tool directly.

### Polish

- Enable Click's built-in shell completion (`lithos --install-completion`).
- `--quiet` / `-q` to suppress progress output (useful in scripts).
- Path arguments complete against the knowledge directory (admin only — client commands work against IDs and content, not local paths).
- No new dependencies — `json` is stdlib, `click` and the MCP SSE client are already present.

## Critical files

- `src/lithos/cli.py` — split into a `cli/` package with `cli/__init__.py` (entrypoint + root group + cross-cutting options), `cli/admin.py`, `cli/client.py`, `cli/_format.py`, `cli/_mcp_client.py`.
- `src/lithos/server.py` — unchanged; reference only, to confirm tool names and response shapes.
- `pyproject.toml` — entrypoint stays `lithos = "lithos.cli:cli"`.
- `tests/test_cli_contract.py` — update to invoke commands under their new paths and exercise both `--output table` and `--output json`.
- `tests/test_cli_client.py` (new) — covers the MCP-backed group against the same SSE fixture used by `tests/test_integration_mcp_sse.py`.
- `docs/cli.md` — rewrite to document the two groups and the cross-cutting flags.

## Reuse, not rewrite

- The MCP SSE client wiring already used by `tests/test_integration_mcp_sse.py` (the `_call_tool` helper and SSE client setup) is the right pattern to lift into `cli/_mcp_client.py` so all `client` subcommands share one connection helper.
- Output formatting helpers in the current `cli.py` (table rendering for tasks/agents/stats) move into `cli/_format.py` so both groups reuse them — the JSON shapes are the same whether the data came from a local `KnowledgeManager` call or an MCP tool response.

## Suggested delivery order

1. Stand up the package skeleton (`cli/__init__.py`, `cli/_format.py`, `cli/_mcp_client.py`) and the `--output` / exit-code conventions.
2. Move the existing direct-access commands under `admin …`.
3. Add `client …` commands one MCP tool at a time, starting with the read-only ones (`read`, `search`, `list`, `stats`, `inspect …`) so JSON output and the SSE client get exercised before any mutating call.
4. Add the mutating `client …` commands (`write`, `delete`, `task …`, `agent …`, `finding …`, `edge …`, `conflict …`).
5. Polish — shell completion, `--quiet`, doc updates.

## Verification

1. `make check` and `make test` to confirm unit/lint/type gates still pass.
2. `make test-integration` — `tests/test_cli_contract.py` exercises the admin group; new tests cover the client group against the SSE fixture.
3. Manual smoke: start `lithos admin serve --transport sse` in one shell, run `lithos client search "foo"`, `lithos client stats`, `lithos client task list` in another, and confirm responses match what the same MCP tools return when called directly via the test harness.
4. Confirm the old top-level commands are gone: `lithos search foo` should fail with click's "No such command" error.
5. JSON contract check: `lithos client search foo --output json` and `lithos client stats --output json` produce parseable JSON whose top-level shape matches the corresponding MCP tool's response envelope.

## Out of scope

- Lockfile/PID-based mutual exclusion between admin commands and a running server.
- Auth on the MCP SSE endpoint (currently unauthenticated; client CLI inherits that).
- Promotion to two separate binaries.
- Streaming output for long-running tool responses.
