# Lithos

**Shared memory for AI agents.**

A local, privacy-first knowledge base that enables heterogeneous AI agents to share knowledge and coordinate work.

## What It Is

Lithos is an MCP server that provides a shared knowledge store for AI agents running on your local infrastructure. Knowledge is stored as human-readable Markdown files (compatible with Obsidian) while providing fast full-text and semantic search for agents.

## Who It's For

- Teams running multiple AI agents (Agent Zero, OpenClaw, Claude Code, custom agents)
- Developers who want agents to share discoveries and avoid duplicate work
- Anyone who needs agent knowledge to be inspectable and version-controlled

## Key Features

- **📁 Markdown-first**: All knowledge stored as Obsidian-compatible `.md` files
- **🔍 Fast search**: Tantivy full-text + ChromaDB semantic search
- **🕸️ Knowledge graph**: NetworkX-powered relationships via `[[wiki-links]]`
- **🤝 Multi-agent coordination**: Task claiming, findings sharing, status tracking
- **🔌 MCP interface**: Works with any MCP-compatible agent or tool
- **🏠 Local & private**: No cloud dependencies, you own your data

## Quickstart

```bash
claude mcp add --transport sse lithos http://localhost:8765/sse
```

## Documentation

- [CLI Reference](docs/cli.md) — installing and using the `lithos` command-line tool
- [Specification](docs/SPECIFICATION.md) — full technical specification
- [LCMA Design](docs/lcma-design.md) — design notes

## Tech Stack

| Component | Technology |
|-----------|------------|
| Storage | Markdown + YAML frontmatter |
| Full-text search | Tantivy |
| Semantic search | ChromaDB + sentence-transformers |
| Knowledge graph | NetworkX |
| Agent interface | MCP (FastMCP) |
| File sync | watchdog |

## Development Commands

```bash
# Install dependencies (uses uv)
uv sync --extra dev

# Run tests
uv run pytest tests/ -v --tb=short

# Run tests with coverage
uv run pytest tests/ --cov=lithos --cov-report=xml

# Lint
uv run ruff check src/ tests/

# Format check
uv run ruff format --check src/ tests/

# Auto-fix lint + format
uv run ruff check --fix src/ tests/ && uv run ruff format src/ tests/

# Start server (stdio)
uv run lithos serve

# Start server (SSE)
uv run lithos serve --transport sse --port 8765

# Docker
cd docker && docker compose up -d --build
```