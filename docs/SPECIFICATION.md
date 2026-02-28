# Lithos - Specification

Version: 0.3.0-draft  
Date: 2026-02-03  
Status: Implementation Ready

---

## 1. Goals

### 1.1 Primary Goals

1. **Shared knowledge store**: Enable multiple heterogeneous AI agents to read and write to a common knowledge base
2. **Human-readable storage**: All knowledge stored as Markdown files that humans can read, edit, and version control
3. **Fast search**: Provide both full-text and semantic search capabilities
4. **Agent coordination**: Allow agents to coordinate work, claim tasks, and share findings
5. **Local-first**: Run entirely on local infrastructure with no external dependencies
6. **MCP interface**: Expose all functionality via Model Context Protocol for broad agent compatibility

### 1.2 Non-Goals

1. **Cloud sync**: No built-in cloud synchronization (use git or other tools externally)
2. **User authentication**: Single-user/single-trust-domain assumed (all agents trusted)
3. **Web UI**: No built-in web interface (use Obsidian or other markdown editors)
4. **Real-time collaboration**: No live cursors or real-time editing (file-based coordination)
5. **Distributed deployment**: Single-node deployment only
6. **Contradictory knowledge resolution**: Agents handle conflicts themselves using confidence scores

---

## 2. Architecture

### 2.1 Component Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                          Lithos                                 │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                    MCP Server (FastMCP)                  │    │
│  │              stdio / SSE transport options               │    │
│  └─────────────────────────────────────────────────────────┘    │
│                              │                                   │
│  ┌───────────────────────────┼───────────────────────────────┐  │
│  │                     Core Services                          │  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐    │  │
│  │  │ Knowledge   │  │   Search    │  │  Coordination   │    │  │
│  │  │  Manager    │  │   Engine    │  │    Service      │    │  │
│  │  └─────────────┘  └─────────────┘  └─────────────────┘    │  │
│  │                   ┌─────────────┐                          │  │
│  │                   │   Agent     │                          │  │
│  │                   │  Registry   │                          │  │
│  │                   └─────────────┘                          │  │
│  └───────────────────────────────────────────────────────────┘  │
│                              │                                   │
│  ┌───────────────────────────┼───────────────────────────────┐  │
│  │                    Storage Layer                           │  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐    │  │
│  │  │  Markdown   │  │  Tantivy    │  │   ChromaDB      │    │  │
│  │  │   Files     │  │  (Index)    │  │   (Vectors)     │    │  │
│  │  └─────────────┘  └─────────────┘  └─────────────────┘    │  │
│  │  ┌─────────────┐  ┌─────────────┐                          │  │
│  │  │  NetworkX   │  │   SQLite    │                          │  │
│  │  │  (Graph)    │  │ (Coord DB)  │                          │  │
│  │  └─────────────┘  └─────────────┘                          │  │
│  └───────────────────────────────────────────────────────────┘  │
│                              │                                   │
│  ┌───────────────────────────┼───────────────────────────────┐  │
│  │                    File Watcher (watchdog)                 │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Data Flow

1. **Write path**: Agent → MCP tool → Knowledge Manager → Write file → File watcher triggers → Update indices
2. **Read path**: Agent → MCP tool → Search Engine → Query indices → Return results
3. **Startup**: Load persisted indices → Scan files for changes (mtime) → Incremental update → Ready

### 2.3 Semantic Search: Chunking Strategy

Documents are chunked on ingest for better semantic search accuracy:

```
┌─────────────────────────────────────────────────────────────┐
│                    Document                                  │
│  "Python asyncio patterns... [2500 chars]"                  │
└─────────────────────────────────────────────────────────────┘
                          │
                    On Ingest
                          ▼
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│  Chunk 1    │  │  Chunk 2    │  │  Chunk 3    │
│  ~500 chars │  │  ~500 chars │  │  ~500 chars │
└─────────────┘  └─────────────┘  └─────────────┘
       │               │               │
       ▼               ▼               ▼
   Embedding 1     Embedding 2     Embedding 3
       │               │               │
       └───────────────┼───────────────┘
                       ▼
              ChromaDB (with doc_id + chunk_index)
```

**Chunking rules:**
- Split on paragraph boundaries (prefer semantic breaks)
- Target ~500 characters per chunk, maximum 1000
- Store `doc_id` + `chunk_index` in ChromaDB metadata
- Semantic search returns chunks, results deduplicated to documents

---

## 3. File Format Specification

### 3.1 Directory Structure

```
data/
├── knowledge/                    # All knowledge files
│   ├── <category>/              # Optional subdirectories for organization
│   │   └── *.md                 # Knowledge files
│   └── *.md                     # Knowledge files
├── coordination.db              # SQLite database for tasks, claims, agents
├── .tantivy/                    # Tantivy index (auto-generated, persistent)
├── .chroma/                     # ChromaDB data (auto-generated, persistent)
└── .graph/                      # NetworkX graph cache (auto-generated)
```

### 3.2 Knowledge File Format

Files use YAML frontmatter + Markdown body, compatible with Obsidian.

```markdown
---
id: <uuid>                        # Required: Unique identifier
created: <ISO 8601 datetime>      # Required: Creation timestamp
updated: <ISO 8601 datetime>      # Required: Last update timestamp
author: <string>                  # Required: Original creator (immutable)
contributors:                     # Optional: List of agents who edited
  - <agent-id-1>
  - <agent-id-2>
tags:                             # Optional: List of tags
  - <tag1>
  - <tag2>
confidence: <float 0-1>           # Optional: Confidence score (default: 1.0)
aliases:                          # Optional: Alternative names (Obsidian compatible)
  - <alias1>
source:                           # Optional: Provenance information
  task: <task-id>                 # Task this was discovered in
  derived_from:                   # IDs of source knowledge
    - <uuid1>
---

# Title

Content in Markdown format.

## Sections as needed

Supports all standard Markdown:
- Lists
- Code blocks
- Tables
- etc.

## Related

- [[other-note]]                  # Wiki-links for relationships
- [[folder/nested-note]]
```

### 3.3 Filename Convention

- Format: `<slug>.md` where slug is URL-safe lowercase with hyphens
- Example: `python-asyncio-patterns.md`
- Subdirectories allowed for organization
- The `id` in frontmatter is the canonical identifier, not the filename

### 3.4 Wiki-Links

- Format: `[[target]]` or `[[target|display text]]`
- Links are parsed and stored in the NetworkX graph

**Resolution precedence (first match wins):**

1. **Exact path**: `[[folder/note]]` → `folder/note.md`
2. **Filename**: `[[note]]` → `*/note.md` (error if ambiguous)
3. **UUID**: `[[550e8400-e29b-41d4-a716-446655440000]]` → file with that `id`
4. **Alias**: `[[my-alias]]` → file with that alias in frontmatter

### 3.5 Author vs Contributors

- **`author`**: Original creator of the document. Immutable after creation. Never appears in `contributors`.
- **`contributors`**: List of agents who have edited the document after creation. Append-only, no duplicates. Does not include the original author.

---

## 4. Agent Identity

### 4.1 Identity Model

Lithos uses a **hybrid agent identity** scheme:

- Agent IDs are **free-form strings** (no mandatory registration)
- System **auto-registers** agents on first interaction
- Optional explicit registration for agents that want to provide metadata

### 4.2 Agent Registry Schema

Stored in `coordination.db`:

```sql
CREATE TABLE agents (
  id TEXT PRIMARY KEY,            -- Free-form identifier, e.g., "agent-zero"
  name TEXT,                      -- Human-friendly display name
  type TEXT,                      -- Agent type: "agent-zero", "openclaw", "claude-code", "custom"
  first_seen_at TIMESTAMP,        -- Auto-set on first interaction
  last_seen_at TIMESTAMP,         -- Updated on each interaction
  metadata JSON                   -- Optional extra info (capabilities, version, etc.)
);
```

### 4.3 Auto-Registration Behavior

On any operation requiring an agent ID (`lithos_write`, `lithos_task_claim`, etc.):

```python
def ensure_agent_known(agent_id: str):
    if not agent_exists(agent_id):
        insert_agent(id=agent_id, first_seen_at=now(), last_seen_at=now())
    else:
        update_agent(id=agent_id, last_seen_at=now())
```

---

## 5. MCP Tools Specification

### 5.1 Knowledge Operations

#### `lithos_write`
Create or update a knowledge file.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `title` | string | Yes | Title of the knowledge item |
| `content` | string | Yes | Markdown content (without frontmatter) |
| `agent` | string | Yes | Your agent identifier |
| `tags` | string[] | No | List of tags |
| `confidence` | float | No | Confidence score 0-1 (default: 1.0) |
| `path` | string | No | Subdirectory path (e.g., "procedures") |
| `id` | string | No | UUID to update existing; omit to create new |
| `source_task` | string | No | Task ID this knowledge came from |
| `derived_from` | string[] | No | IDs of source knowledge items |

**Returns:** `{ id: string, path: string }`

**Behavior on update:** If `id` is provided and exists, the agent is added to `contributors` if not already present.

#### `lithos_read`
Read a knowledge file by ID or path.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `id` | string | No* | UUID of knowledge item |
| `path` | string | No* | File path relative to knowledge/ |
| `max_length` | int | No | Truncate content to N characters (default: unlimited) |

*One of `id` or `path` required.

**Returns:** `{ id, title, content, metadata, links, truncated: boolean }`

**Truncation behavior:** When `max_length` is specified, content is truncated at the nearest paragraph or sentence boundary at or before the limit. Returns `truncated: true` if content was shortened.

#### `lithos_delete`
Delete a knowledge file.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `id` | string | Yes | UUID of knowledge item to delete |
| `agent` | string | No | Agent performing deletion (for audit trail) |

**Returns:** `{ success: boolean }`

#### `lithos_search`
Full-text search across knowledge base.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `query` | string | Yes | Search query (Tantivy query syntax) |
| `limit` | int | No | Max results (default: 10) |
| `tags` | string[] | No | Filter by tags (AND) |
| `author` | string | No | Filter by author |
| `path_prefix` | string | No | Filter by path prefix |

**Returns:** `{ results: [{ id, title, snippet, score, path }] }`

**Snippet source:** Tantivy-generated highlight showing matching terms in context.

#### `lithos_semantic`
Semantic similarity search.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `query` | string | Yes | Natural language query |
| `limit` | int | No | Max results (default: 10) |
| `threshold` | float | No | Minimum similarity 0-1 (default: 0.5) |
| `tags` | string[] | No | Filter by tags |

**Returns:** `{ results: [{ id, title, snippet, similarity, path }] }`

**Snippet source:** Content of the best-matching chunk for each document.

**Note:** Search operates on chunks internally but returns deduplicated documents.

#### `lithos_list`
List knowledge items with filters.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `path_prefix` | string | No | Filter by path prefix |
| `tags` | string[] | No | Filter by tags |
| `author` | string | No | Filter by author |
| `since` | string | No | Filter by updated date (ISO 8601) |
| `limit` | int | No | Max results (default: 50) |
| `offset` | int | No | Pagination offset |

**Returns:** `{ items: [{ id, title, path, updated, tags }], total: int }`

### 5.2 Graph Operations

#### `lithos_links`
Get links for a knowledge item.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `id` | string | Yes | UUID of knowledge item |
| `direction` | string | No | "outgoing", "incoming", or "both" (default: "both") |
| `depth` | int | No | Traversal depth (default: 1, max: 3) |

**Returns:** `{ outgoing: [{ id, title }], incoming: [{ id, title }] }`

**Multi-hop behavior:** Returns flat lists regardless of depth. For `depth > 1`, results include all reachable nodes within N hops, deduplicated. Path information is not preserved.

#### `lithos_tags`
List all tags or items with specific tags.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `tag` | string | No | Get items with this tag; omit to list all tags |

**Returns:** `{ tags: [{ name, count }] }` or `{ items: [{ id, title }] }`

### 5.3 Agent Operations

#### `lithos_agent_register`
Explicitly register an agent with metadata (optional, agents are auto-registered on first use).

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `id` | string | Yes | Agent identifier |
| `name` | string | No | Human-friendly display name |
| `type` | string | No | Agent type ("agent-zero", "openclaw", "claude-code", "custom") |
| `metadata` | object | No | Additional metadata (capabilities, version, etc.) |

**Returns:** `{ success: boolean, created: boolean }`

**Response semantics:**
- `{ success: true, created: true }` — New agent registered
- `{ success: true, created: false }` — Agent already existed, metadata updated, `last_seen_at` refreshed

#### `lithos_agent_info`
Get information about an agent.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `id` | string | Yes | Agent identifier |

**Returns:** `{ id, name, type, first_seen_at, last_seen_at, metadata }`

#### `lithos_agent_list`
List all known agents.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `type` | string | No | Filter by agent type |
| `active_since` | string | No | Only agents seen since (ISO 8601) |

**Returns:** `{ agents: [{ id, name, type, last_seen_at }] }`

### 5.4 Coordination Operations

#### `lithos_task_create`
Create a coordination task.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `title` | string | Yes | Task title |
| `description` | string | No | Task description |
| `tags` | string[] | No | Task tags |
| `agent` | string | Yes | Creating agent identifier |

**Returns:** `{ task_id: string }`

#### `lithos_task_claim`
Claim an aspect of a task.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Task ID |
| `aspect` | string | Yes | What aspect you're working on |
| `agent` | string | Yes | Your agent identifier |
| `ttl_minutes` | int | No | Claim duration (default: 60, max: 480) |

**Returns:** `{ success: boolean, expires_at: string }`

#### `lithos_task_renew`
Extend an existing task claim.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Task ID |
| `aspect` | string | Yes | The aspect claim to renew |
| `agent` | string | Yes | Your agent identifier |
| `ttl_minutes` | int | No | New duration from now (default: 60, max: 480) |

**Returns:** `{ success: boolean, new_expires_at: string }`

**Note:** Only the agent holding the claim can renew it.

#### `lithos_task_release`
Release a task claim.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Task ID |
| `aspect` | string | Yes | The aspect claim to release |
| `agent` | string | Yes | Your agent identifier |

**Returns:** `{ success: boolean }`

#### `lithos_task_complete`
Mark a task as completed.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Task ID |
| `agent` | string | Yes | Agent marking completion |

**Returns:** `{ success: boolean }`

**Behavior:** Sets task status to 'completed' and releases all active claims on the task.

#### `lithos_task_status`
Get task status and claims.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | No | Specific task; omit for all active tasks |

**Returns:** `{ tasks: [{ id, title, status, claims: [{ agent, aspect, expires_at }] }] }`

**Claim expiry handling:** Expired claims (where `expires_at < now()`) are automatically excluded from results. Cleanup is lazy—expired claims are filtered at query time rather than eagerly deleted.

#### `lithos_finding_post`
Post a finding to a task.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Task ID |
| `agent` | string | Yes | Your agent identifier |
| `summary` | string | Yes | Brief summary of finding |
| `knowledge_id` | string | No | Link to knowledge item if created |

**Returns:** `{ finding_id: string }`

#### `lithos_finding_list`
List findings for a task.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Task ID |
| `since` | string | No | Only findings after this time |

**Returns:** `{ findings: [{ id, agent, summary, knowledge_id, timestamp }] }`

### 5.5 System Operations

#### `lithos_stats`
Get knowledge base statistics.

**Arguments:** None

**Returns:**
```json
{
  "documents": 1234,
  "chunks": 5678,
  "agents": 5,
  "active_tasks": 12,
  "open_claims": 8,
  "tags": 89
}
```

**Use case:** Allows agents to understand knowledge base scale before issuing broad queries.

---

## 6. Index Behavior

### 6.1 Startup (Incremental Loading)

1. Load persisted Tantivy index from `.tantivy/`
2. Load persisted ChromaDB from `.chroma/`
3. Load or rebuild NetworkX graph from `.graph/` cache
4. Scan `knowledge/` directory for file changes:
   - Compare file `mtime` against last indexed time
   - Add new files to indices
   - Update modified files in indices
   - Remove deleted files from indices
5. Load coordination state from `coordination.db`
6. Start file watcher

**Full rebuild** only when forced via `lithos reindex --force`.

### 6.2 File Change Handling

| Event | Action |
|-------|--------|
| File created | Parse, chunk, add to all indices |
| File modified | Parse, re-chunk, update all indices |
| File deleted | Remove from all indices |
| File moved/renamed | Parse new file, match by UUID in frontmatter, update path in indices |

**Note on renames and wiki-links:** When a file is renamed, UUID matching preserves identity in indices. However, wiki-link text in *other* files still points to the old path. `lithos validate` reports these as broken links.

### 6.3 Index Persistence

- **Tantivy**: Persisted to `.tantivy/` directory
- **ChromaDB**: Persisted to `.chroma/` directory  
- **NetworkX**: Cached to `.graph/graph.pickle`, rebuilt if missing

---

## 7. Coordination Database Schema

Stored in `coordination.db` (SQLite, accessed via `aiosqlite` for async compatibility):

```sql
-- Agent registry
CREATE TABLE agents (
  id TEXT PRIMARY KEY,
  name TEXT,
  type TEXT,
  first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  metadata JSON
);

-- Tasks
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT,
  status TEXT DEFAULT 'open',  -- open, completed, cancelled
  created_by TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  tags JSON
);

-- Claims (with automatic expiry)
CREATE TABLE claims (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  agent TEXT NOT NULL,
  aspect TEXT NOT NULL,
  claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  expires_at TIMESTAMP NOT NULL,
  FOREIGN KEY (task_id) REFERENCES tasks(id),
  UNIQUE(task_id, aspect)  -- One agent per aspect
);

-- Findings
CREATE TABLE findings (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  agent TEXT NOT NULL,
  summary TEXT NOT NULL,
  knowledge_id TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (task_id) REFERENCES tasks(id)
);
```

---

## 8. Configuration

### 8.1 Configuration File

Location: `config.yaml` in data directory or specified via `--config`.

```yaml
# Server configuration
server:
  transport: stdio          # stdio | sse
  host: 127.0.0.1          # For SSE transport
  port: 8765               # For SSE transport

# Storage paths
storage:
  data_dir: ./data         # Base data directory
  knowledge_dir: knowledge # Relative to data_dir

# Search configuration  
search:
  embedding_model: all-MiniLM-L6-v2  # sentence-transformers model
  semantic_threshold: 0.5   # Default similarity threshold
  max_results: 50           # Maximum search results
  chunk_size: 500           # Target chunk size in characters
  chunk_max: 1000           # Maximum chunk size

# Coordination
coordination:
  claim_ttl_minutes: 60     # Default claim duration
  claim_max_ttl_minutes: 480 # Maximum claim duration

# Indexing
index:
  rebuild_on_start: false   # Force rebuild indices on startup
  watch_debounce_ms: 500    # Debounce file changes
```

### 8.2 Command Line Interface

```bash
# Run with stdio transport (for MCP)
lithos serve --transport stdio --data-dir ./data

# Run with SSE transport (for HTTP access)
lithos serve --transport sse --host 127.0.0.1 --port 8765 --data-dir ./data

# Rebuild indices (incremental by default)
lithos reindex --data-dir ./data

# Force full rebuild
lithos reindex --data-dir ./data --force

# Validate knowledge files
lithos validate --data-dir ./data
# Reports: broken [[wiki-links]], missing frontmatter, ambiguous links, stale references after renames
```

---

## 9. Error Handling

### 9.1 MCP Error Responses

All tools return errors in MCP-standard format:

```json
{
  "error": {
    "code": "<error_code>",
    "message": "<human readable message>"
  }
}
```

### 9.2 Error Codes

| Code | Description |
|------|-------------|
| `NOT_FOUND` | Knowledge item, task, or agent not found |
| `ALREADY_EXISTS` | Item with same ID already exists |
| `INVALID_FORMAT` | Invalid file format or frontmatter |
| `CLAIM_CONFLICT` | Task aspect already claimed by another agent |
| `CLAIM_NOT_FOUND` | No active claim to renew/release |
| `CLAIM_NOT_OWNED` | Attempting to renew/release another agent's claim |
| `VALIDATION_ERROR` | Invalid arguments |
| `INDEX_ERROR` | Search index error |
| `AMBIGUOUS_LINK` | Wiki-link matches multiple files |

---

## 10. Success Criteria

### 10.1 Functional Requirements

- [ ] Create, read, update, delete knowledge files via MCP
- [ ] Full-text search returns relevant results in <100ms for <10k documents
- [ ] Semantic search returns relevant results in <500ms for <10k documents
- [ ] Chunked embeddings improve semantic search accuracy for long documents
- [ ] Wiki-links parsed and queryable via graph operations
- [ ] File changes detected and indices updated within 2 seconds
- [ ] File renames preserve document identity via UUID matching
- [ ] Task coordination prevents duplicate claims (atomic via SQLite)
- [ ] Agent auto-registration on first interaction
- [ ] Works with Agent Zero via MCP (stdio)
- [ ] Works with OpenClaw via MCP (SSE)
- [ ] Knowledge files readable in Obsidian without modification
- [ ] `lithos validate` reports broken wiki-links including stale references after renames
- [ ] `lithos_stats` returns accurate counts

### 10.2 Non-Functional Requirements

- [ ] Single Python process, no external services required
- [ ] Startup time <10 seconds for 1000 documents (incremental loading)
- [ ] Memory usage <1GB for 10k documents
- [ ] All data recoverable from markdown files alone (indices can be rebuilt)
- [ ] Async SQLite access does not block MCP event loop

---

## 11. Dependencies

### 11.1 Python Packages

```
fastmcp>=2.0.0           # MCP server framework
tantivy>=0.22.0          # Full-text search
chromadb>=0.4.0          # Vector database
sentence-transformers>=2.2.0  # Embeddings
networkx>=3.0            # Graph operations
watchdog>=3.0.0          # File watching
pyyaml>=6.0              # YAML parsing
python-frontmatter>=1.0.0 # Markdown frontmatter
aiofiles>=23.0.0         # Async file operations
aiosqlite>=0.17.0        # Async SQLite access
```

### 11.2 Python Version

- Minimum: Python 3.10
- Recommended: Python 3.11+

---

## 12. Development

### 12.1 Project Structure

```
lithos/
├── src/
│   └── lithos/
│       ├── __init__.py
│       ├── server.py          # FastMCP server entry point
│       ├── knowledge.py       # Knowledge CRUD operations
│       ├── search.py          # Tantivy + ChromaDB search
│       ├── graph.py           # NetworkX graph operations
│       ├── coordination.py    # SQLite tasks/claims/agents
│       ├── config.py          # Configuration management
│       └── cli.py             # CLI commands
├── tests/
│   ├── conftest.py
│   ├── test_knowledge.py
│   ├── test_search.py
│   ├── test_graph.py
│   ├── test_coordination.py
│   └── test_integration.py
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
├── data/                      # Default data directory (gitignored)
│   └── .gitkeep
├── pyproject.toml             # Build config, dependencies, tool config
├── uv.lock                    # Locked dependencies
├── README.md
├── SPECIFICATION.md
├── LICENSE
└── .github/
    └── workflows/
        └── ci.yml             # Lint, format check, tests
```

### 12.2 Build System: Hatch

Lithos uses [Hatch](https://hatch.pypa.io/) as the build system:

- PEP 517/621 compliant
- Manages project metadata in `pyproject.toml`
- Builds wheels and sdists
- Integrates with uv for fast dependency resolution

```toml
# pyproject.toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "lithos"
version = "0.1.0"
description = "Local shared knowledge base for AI agents"
readme = "README.md"
license = "MIT"
requires-python = ">=3.10"
authors = [
    { name = "Your Name", email = "you@example.com" }
]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
]
dependencies = [
    "fastmcp>=2.0.0",
    "tantivy>=0.22.0",
    "chromadb>=0.4.0",
    "sentence-transformers>=2.2.0",
    "networkx>=3.0",
    "watchdog>=3.0.0",
    "pyyaml>=6.0",
    "python-frontmatter>=1.0.0",
    "aiofiles>=23.0.0",
    "aiosqlite>=0.17.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.0.0",
    "pytest-asyncio>=0.21.0",
    "pytest-cov>=4.0.0",
    "ruff>=0.1.0",
]

[project.scripts]
lithos = "lithos.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["src/lithos"]

[tool.hatch.envs.default]
installer = "uv"
```

### 12.3 Dependency Management: uv

Lithos uses [uv](https://github.com/astral-sh/uv) for fast dependency management:

- 10-100x faster than pip
- Drop-in pip replacement
- Rust-based, by Astral (same team as ruff)
- Generates reproducible lockfiles

**Commands:**

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create virtual environment and install dependencies
uv venv
uv pip install -e ".[dev]"

# Lock dependencies
uv pip compile pyproject.toml -o uv.lock

# Sync from lockfile
uv pip sync uv.lock

# Add a new dependency
uv pip install <package>
```

### 12.4 Code Quality: Ruff

Lithos uses [Ruff](https://github.com/astral-sh/ruff) for linting and formatting:

- Extremely fast (Rust-based)
- Replaces black, flake8, isort, and more
- Single tool for all code quality checks

```toml
# pyproject.toml
[tool.ruff]
line-length = 100
target-version = "py310"
src = ["src", "tests"]

[tool.ruff.lint]
select = [
    "E",      # pycodestyle errors
    "F",      # pyflakes
    "I",      # isort
    "UP",     # pyupgrade
    "B",      # flake8-bugbear
    "SIM",    # flake8-simplify
    "RUF",    # ruff-specific
]
ignore = [
    "E501",   # line too long (handled by formatter)
]

[tool.ruff.lint.isort]
known-first-party = ["lithos"]

[tool.ruff.format]
quote-style = "double"
indent-style = "space"
skip-magic-trailing-comma = false
```

**Commands:**

```bash
# Check for lint errors
ruff check src/ tests/

# Fix auto-fixable lint errors
ruff check --fix src/ tests/

# Format code
ruff format src/ tests/

# Check formatting without changes
ruff format --check src/ tests/
```

### 12.5 Docker Deployment

Lithos runs in Docker for consistent deployment, matching Agent Zero's setup.

**Dockerfile:**

```dockerfile
# docker/Dockerfile
FROM python:3.11-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Set working directory
WORKDIR /app

# Copy project files
COPY pyproject.toml uv.lock ./
COPY src/ ./src/

# Install dependencies
RUN uv venv /opt/venv &&     . /opt/venv/bin/activate &&     uv pip sync uv.lock &&     uv pip install -e .

# Set environment
ENV PATH="/opt/venv/bin:$PATH"
ENV LITHOS_DATA_DIR=/data

# Create data directory
RUN mkdir -p /data/knowledge

# Expose SSE port
EXPOSE 8765

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3     CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/health')" || exit 1

# Run server
CMD ["lithos", "serve", "--transport", "sse", "--host", "0.0.0.0", "--port", "8765"]
```

**docker-compose.yml:**

```yaml
# docker/docker-compose.yml
version: "3.8"

services:
  lithos:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    container_name: lithos
    restart: unless-stopped
    volumes:
      - lithos-data:/data
    ports:
      - "8765:8765"
    environment:
      - LITHOS_DATA_DIR=/data
      - LITHOS_TRANSPORT=sse
      - LITHOS_HOST=0.0.0.0
      - LITHOS_PORT=8765
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8765/health')"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s

volumes:
  lithos-data:
    driver: local
```

**Usage:**

```bash
# Build and start
cd docker
docker-compose up -d --build

# View logs
docker-compose logs -f lithos

# Stop
docker-compose down

# Stop and remove data
docker-compose down -v
```

### 12.6 Integration with Agent Zero

To connect Lithos with Agent Zero running in Docker:

```yaml
# docker-compose.yml (combined setup)
version: "3.8"

services:
  agent-zero:
    image: agent0ai/agent-zero
    ports:
      - "80:80"
    volumes:
      - ./a0-data:/a0
    environment:
      - MCP_SERVERS=lithos:http://lithos:8765
    depends_on:
      - lithos

  lithos:
    build:
      context: ./lithos
      dockerfile: docker/Dockerfile
    volumes:
      - lithos-data:/data
    expose:
      - "8765"
    environment:
      - LITHOS_TRANSPORT=sse
      - LITHOS_HOST=0.0.0.0

volumes:
  lithos-data:
```

### 12.7 CI/CD

**GitHub Actions workflow:**

```yaml
# .github/workflows/ci.yml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install uv
        uses: astral-sh/setup-uv@v1
      - name: Set up Python
        run: uv python install 3.11
      - name: Install dependencies
        run: uv pip install ruff
      - name: Lint
        run: ruff check src/ tests/
      - name: Format check
        run: ruff format --check src/ tests/

  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install uv
        uses: astral-sh/setup-uv@v1
      - name: Set up Python
        run: uv python install 3.11
      - name: Install dependencies
        run: |
          uv venv
          uv pip install -e ".[dev]"
      - name: Run tests
        run: |
          source .venv/bin/activate
          pytest tests/ -v --cov=lithos --cov-report=xml
      - name: Upload coverage
        uses: codecov/codecov-action@v3
        with:
          files: coverage.xml

  docker:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build Docker image
        run: docker build -f docker/Dockerfile -t lithos:test .
      - name: Test Docker image
        run: |
          docker run -d --name lithos-test -p 8765:8765 lithos:test
          sleep 10
          curl -f http://localhost:8765/health || exit 1
          docker stop lithos-test
```

### 12.8 Development Workflow

```bash
# 1. Clone repository
git clone https://github.com/yourname/lithos.git
cd lithos

# 2. Install uv (if needed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. Create environment and install
uv venv
source .venv/bin/activate  # or `.venv\Scripts\activate` on Windows
uv pip install -e ".[dev]"

# 4. Run linting/formatting
ruff check src/ tests/
ruff format src/ tests/

# 5. Run tests
pytest tests/ -v

# 6. Run locally
lithos serve --transport stdio

# 7. Run in Docker
cd docker
docker-compose up -d --build
```

---

## 13. Future Considerations (Out of Scope for v0.1)

These are explicitly not part of the initial implementation but may be considered later:

- Web UI for browsing knowledge
- Agent Zero memory sync/bridge
- Knowledge versioning (beyond git)
- Multi-node deployment
- Access control / namespaces
- Knowledge expiration / TTL
- Automated knowledge quality scoring
- Contradictory knowledge resolution
- Integration with external knowledge sources
- Full edit history / provenance log
- `lithos_task_cancel` tool
- Hierarchical multi-hop link results

---

## Appendix A: Example Session

```
# Check knowledge base stats
→ lithos_stats()
← { documents: 0, chunks: 0, agents: 0, active_tasks: 0, open_claims: 0, tags: 0 }

# Agent Zero registers (optional, would auto-register anyway)
→ lithos_agent_register(id="agent-zero", name="Agent Zero", type="agent-zero")
← { success: true, created: true }

# Agent Zero stores a discovery
→ lithos_write(title="Python asyncio.gather patterns", content="...", tags=["python", "async"], agent="agent-zero")
← { id: "abc-123", path: "python-asyncio-gather-patterns.md" }

# OpenClaw searches for async knowledge (semantic search uses chunks internally)
→ lithos_semantic(query="how to run async tasks concurrently in python")
← { results: [{ id: "abc-123", title: "Python asyncio.gather patterns", similarity: 0.89, snippet: "...best matching chunk..." }] }

# OpenClaw reads with truncation to avoid context flooding
→ lithos_read(id="abc-123", max_length=2000)
← { id: "abc-123", title: "...", content: "...[truncated at sentence boundary]", truncated: true }

# Create a research task
→ lithos_task_create(title="Research async patterns", agent="agent-zero")
← { task_id: "task-456" }

# Agent claims research task
→ lithos_task_claim(task_id="task-456", aspect="literature review", agent="agent-zero")
← { success: true, expires_at: "2026-02-03T22:00:00Z" }

# Agent renews claim for long-running work
→ lithos_task_renew(task_id="task-456", aspect="literature review", agent="agent-zero", ttl_minutes=120)
← { success: true, new_expires_at: "2026-02-04T00:00:00Z" }

# Another agent checks what's being worked on
→ lithos_task_status(task_id="task-456")
← { tasks: [{ id: "task-456", status: "open", claims: [{ agent: "agent-zero", aspect: "literature review", expires_at: "..." }] }] }

# Complete the task
→ lithos_task_complete(task_id="task-456", agent="agent-zero")
← { success: true }

# List all known agents
→ lithos_agent_list()
← { agents: [{ id: "agent-zero", name: "Agent Zero", last_seen_at: "..." }, { id: "openclaw", ... }] }

# Check updated stats
→ lithos_stats()
← { documents: 1, chunks: 3, agents: 2, active_tasks: 0, open_claims: 0, tags: 2 }
```

---

## Appendix B: Tool Summary

| Category | Tools |
|----------|-------|
| Knowledge | `lithos_write`, `lithos_read`, `lithos_delete`, `lithos_search`, `lithos_semantic`, `lithos_list` |
| Graph | `lithos_links`, `lithos_tags` |
| Agent | `lithos_agent_register`, `lithos_agent_info`, `lithos_agent_list` |
| Coordination | `lithos_task_create`, `lithos_task_claim`, `lithos_task_renew`, `lithos_task_release`, `lithos_task_complete`, `lithos_task_status`, `lithos_finding_post`, `lithos_finding_list` |
| System | `lithos_stats` |

**Total: 19 MCP tools**

---

**End of Specification**
