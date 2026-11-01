# Lithos Quick Guide

## Project Overview

Lithos is a local, privacy-first MCP server that provides a shared knowledge base for AI agents. Knowledge is stored as Obsidian-compatible Markdown files with YAML frontmatter, searchable via Tantivy (full-text) and ChromaDB (semantic). A NetworkX knowledge graph tracks `[[wiki-link]]` relationships. Agents coordinate via SQLite-backed task claiming and findings.

## Tech Stack

- **Python 3.10+** with `src/lithos/` layout (hatchling build)
- **FastMCP** for the MCP server interface (stdio and SSE transports)
- **Tantivy** for full-text search, **ChromaDB + sentence-transformers** for semantic search
- **NetworkX** for knowledge graph, **watchdog** for file sync
- **Pydantic + pydantic-settings** for configuration
- **Click** for CLI
- **Docker** multi-stage build in `docker/`

## Done Criteria (Required)

A change is **not done** unless all five are green:

1. Unit tests: `make test`
2. Integration tests: `make test-integration`
3. Lint + format: `make lint`
4. Type check: `make typecheck`
5. All checks (1, 3, 4): `make check`

## Code Conventions

- **Pyright** for type checking (standard mode)
- **Ruff** for linting and formatting (line-length 100, double quotes, spaces)
- Lint rules: E, F, I (isort), UP, B, SIM, RUF
- Async throughout: tests use `pytest-asyncio` with `asyncio_mode = "auto"`
- All MCP tools prefixed `lithos_` (e.g., `lithos_write`, `lithos_search`, `lithos_task_create`)
- Config via `LithosConfig` pydantic-settings model; env vars use `LITHOS_` prefix
- Tests use temp directories via `test_config` fixture; always clean up

## Architecture guardrails & generated docs

`docs/generated/` holds generated views of the code — component diagram, domain
model, architecture metrics, MCP tool catalog, container/data-store view, and
per-component drill-down pages (indexed by `docs/generated/README.md`) — produced
by `tests/guardrail/` and drift-checked in CI:

- `make diagrams` regenerates everything (it just runs `pytest tests/guardrail/ -q`).
  Note `make test` runs the same tests, so a test run rewrites `docs/generated/`
  as a side effect — commit the result if it changed.
- The CI job `Diagram drift` fails when the committed files disagree with what
  the code generates. Fix: `make diagrams`, commit.
- `docs/architecture.toml` is the source of truth for components, tiers, and
  domain-model scanning. Adding a new module or component? The guardrail orphan
  checks will fail until you map it there.
- Directional import rules (Entrypoints → Core → Foundation) are enforced by
  import-linter (`pyproject.toml [tool.importlinter]`); the `lithos.lcma` seam
  by `tests/test_module_boundaries.py`.

## Agent skills

### Issue tracker

Issues and PRDs live as GitHub issues on `agent-lore/lithos` (use `gh`). See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical roles (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`) using default label strings. See `docs/agents/triage-labels.md`.

### Runbooks

Operator procedures live in `docs/runbooks/` (e.g. evaluating and migrating the adjudication LLM: `docs/runbooks/llm-model-migration.md`).

### Domain docs

Single-context — one `CONTEXT.md` + `docs/adr/` at the repo root (created lazily). See `docs/agents/domain.md`.

## Guardrail policy

Guardrails support maintainability; passing a metric alone is not evidence of
good design. Dependency restrictions, cycle limits, and private-access contracts
are architectural invariants. Size, complexity, and dependency counts are review
thresholds: do not split cohesive code or add forwarding layers just to meet a
number. Refactor when a clean seam improves clarity, cohesion, or testability.

`[policies]` classifies the accepted budgets; `[review]` holds per-entity thresholds
and optional new/worsened-code gates. If keeping code together is clearer, record
a proportionate, bounded `[[exceptions]]` entry with a reason and review reference,
or explain a threshold adjustment in the same PR. Never automatically raise
budgets or bypass an invariant. Lower thresholds when that protects useful gains.
Changed-code gates remain disabled until the first 1.6 baseline is merged.
See `docs/quality-guide.md` for the migration and evidence contracts.
