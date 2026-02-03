# Exogram

**Shared memory for AI agents.**

A local, privacy-first knowledge base that enables heterogeneous AI agents to share knowledge and coordinate work.

## What It Is

Exogram is an MCP server that provides a shared knowledge store for AI agents running on your local infrastructure. Knowledge is stored as human-readable Markdown files (compatible with Obsidian) while providing fast full-text and semantic search for agents.

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

## Tech Stack

| Component | Technology |
|-----------|------------|
| Storage | Markdown + YAML frontmatter |
| Full-text search | Tantivy |
| Semantic search | ChromaDB + sentence-transformers |
| Knowledge graph | NetworkX |
| Agent interface | MCP (FastMCP) |
| File sync | watchdog |
