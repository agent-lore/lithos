# CLAUDE.md

## Project Overview

Exogram is a local, privacy-first MCP server that provides a shared knowledge base for AI agents. Knowledge is stored as Obsidian-compatible Markdown files with YAML frontmatter, searchable via Tantivy (full-text) and ChromaDB (semantic). A NetworkX knowledge graph tracks `[[wiki-link]]` relationships. Agents coordinate via SQLite-backed task claiming and findings.

## Tech Stack

- **Python 3.10+** with `src/exogram/` layout (hatchling build)
- **FastMCP** for the MCP server interface (stdio and SSE transports)
- **Tantivy** for full-text search, **ChromaDB + sentence-transformers** for semantic search
- **NetworkX** for knowledge graph, **watchdog** for file sync
- **Pydantic + pydantic-settings** for configuration
- **Click** for CLI
- **Docker** multi-stage build in `docker/`

## Development Commands

```bash
# Install dependencies (uses uv)
uv sync --extra dev

# Run tests
uv run pytest tests/ -v --tb=short

# Run tests with coverage
uv run pytest tests/ --cov=exogram --cov-report=xml

# Lint
uv run ruff check src/ tests/

# Format check
uv run ruff format --check src/ tests/

# Auto-fix lint + format
uv run ruff check --fix src/ tests/ && uv run ruff format src/ tests/

# Start server (stdio)
uv run exogram serve

# Start server (SSE)
uv run exogram serve --transport sse --port 8765

# Docker
cd docker && docker compose up -d --build
```

## Project Structure

```
src/exogram/
  server.py       # ExogramServer: FastMCP app, tool registration, file watcher
  cli.py          # Click CLI: serve, reindex, validate, stats, search
  config.py       # Pydantic config (ExogramConfig), env vars (EXOGRAM_* prefix)
  knowledge.py    # KnowledgeManager: CRUD for markdown files with frontmatter
  search.py       # SearchEngine: Tantivy full-text + ChromaDB semantic search
  graph.py        # KnowledgeGraph: NetworkX graph of [[wiki-links]]
  coordination.py # CoordinationService: SQLite-backed tasks, claims, findings
tests/
  conftest.py     # Shared fixtures (temp dirs, test config, sample data)
  test_*.py       # One test file per module
docker/
  Dockerfile      # Multi-stage build (Python 3.11-slim)
  docker-compose.yml
```

## Code Conventions

- **Ruff** for linting and formatting (line-length 100, double quotes, spaces)
- Lint rules: E, F, I (isort), UP, B, SIM, RUF
- Async throughout: tests use `pytest-asyncio` with `asyncio_mode = "auto"`
- All MCP tools prefixed `exo_` (e.g., `exo_write`, `exo_search`, `exo_task_create`)
- Config via `ExogramConfig` pydantic-settings model; env vars use `EXOGRAM_` prefix
- Tests use temp directories via `test_config` fixture; always clean up

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on push to main/develop and PRs to main:
1. **Lint** — ruff check + format check
2. **Test** — pytest + coverage upload
3. **Docker Build** — build image without push
4. **Integration** — docker compose up, test SSE endpoint, MCP client connection
