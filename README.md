# Lithos

**Shared memory for AI agents.**

A local, privacy-first knowledge base that enables heterogeneous AI agents to share knowledge and coordinate work.

## The Problem

When agents cannot share what they know, every agent starts from zero. Work is duplicated, discoveries are lost, and coordination breaks down. Lithos solves this by providing a **persistent, shared knowledge layer** that compounds in value over time.

## What It Is

Lithos is an MCP server that provides a shared knowledge store for AI agents running on your local infrastructure. Knowledge is stored as human-readable Markdown files (compatible with Obsidian) while providing fast full-text and semantic search for agents.

## Who It's For

Lithos is the **Knowledge Layer** for teams running AI agents in production.

Just as Alation coined the term "Knowledge Layer" for enterprise data governance, Lithos provides the equivalent for AI agent systems: a structured, searchable, shared memory that **compounds in value** the more it is used. Each agent interaction enriches the knowledge base, making every subsequent agent smarter and faster.

- Teams running multiple AI agents (Agent Zero, OpenClaw, Claude Code, custom agents)
- Developers who want agents to share discoveries and avoid duplicate work
- Anyone who needs agent knowledge to be inspectable and version-controlled

## Key Features

- **📁 Markdown-first**: All knowledge stored as Obsidian-compatible `.md` files
- **🔍 Fast search**: Tantivy full-text + ChromaDB semantic search
- **🕸️ Knowledge graph**: NetworkX-powered relationships via `[[wiki-links]]`
- **🤝 Multi-agent coordination**: Task claiming, findings sharing, status tracking
- **🧠 Research cache**: One-call freshness check so agents skip redundant research — returns hit/miss/stale with update guidance
- **🔗 URL deduplication**: Automatic detection and prevention of duplicate notes from the same source URL
- **🧬 Provenance tracking**: Declare which notes a synthesis is derived from and query lineage across the knowledge base
- **🔌 MCP interface**: Works with any MCP-compatible agent or tool
- **🏠 Local & private**: No cloud dependencies, you own your data

## Quickstart

### Agent Zero

Agent Zero running inside docker on the same machine running lithos

```json
{
  "mcpServers": {
    "lithos": {
      "url": "http://host.docker.internal:8765/sse"
    }
  }
}
```

### OpenClaw

Update `mcporter.json` Probably in `~/.openclaw/workspace/config/mcporter.json`

Use `localhost` if running on the same machine as OpenClaw, otherwise the name or IP address of the server.

```json
{
  "mcpServers": {
    "lithos": {
      "baseUrl": "http://<your hostname>:8765/sse"
    }
  },
  "imports": []
}
```

### Claude code

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

# Run unit tests
uv run --extra dev pytest -m "not integration" tests/ -q

# Run integration tests
uv run --extra dev pytest -m integration tests/ -q

# Run all tests with coverage
uv run --extra dev pytest tests/ --cov=lithos --cov-report=xml

# Lint
uv run --extra dev ruff check .

# Format check
uv run --extra dev ruff format --check src/ tests/

# Type check
uv run --extra dev pyright src/

# Auto-fix lint + format
uv run --extra dev ruff check --fix . && uv run --extra dev ruff format src/ tests/

# Start server (stdio)
uv run lithos serve

# Start server (SSE)
uv run lithos serve --transport sse --port 8765

# Docker
cd docker && docker compose up -d --build

# run pointing at data dir
LITHOS_DATA_PATH="<DATA DIR PATH>" docker compose up -d --build

# stop
cd docker && docker compose down
```