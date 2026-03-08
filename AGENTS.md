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

1. Unit tests: `uv run --extra dev pytest -m "not integration" tests/ -q`
2. Integration tests: `uv run --extra dev pytest -m integration tests/ -q`
3. Lint: `uv run --extra dev ruff check .`
4. Format check: `uv run --extra dev ruff format --check src/ tests/`
5. Type check: `uv run --extra dev pyright src/`

## Code Conventions

- **Pyright** for type checking (standard mode)
- **Ruff** for linting and formatting (line-length 100, double quotes, spaces)
- Lint rules: E, F, I (isort), UP, B, SIM, RUF
- Async throughout: tests use `pytest-asyncio` with `asyncio_mode = "auto"`
- All MCP tools prefixed `lithos_` (e.g., `lithos_write`, `lithos_search`, `lithos_task_create`)
- Config via `LithosConfig` pydantic-settings model; env vars use `LITHOS_` prefix
- Tests use temp directories via `test_config` fixture; always clean up