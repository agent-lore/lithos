# Lithos - Specification

Version: 0.4.0
Date: 2026-02-28
Status: Aligned with Implementation

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
├── knowledge/                    # Authoritative content (Markdown + frontmatter)
│   ├── <category>/              # Optional subdirectories for organization
│   │   └── *.md
│   └── *.md
├── .lithos/                     # Authoritative state (cannot be rebuilt — back up)
│   └── coordination.db          # SQLite: tasks, claims, agents, findings
├── .tantivy/                    # Rebuildable index (full-text search)
├── .chroma/                     # Rebuildable index (semantic embeddings)
└── .graph/                      # Rebuildable cache (wiki-link graph)
```

**Authoritative vs. rebuildable:** `knowledge/` and `.lithos/` contain data that cannot be regenerated — they must be backed up and preserved. The index directories (`.tantivy/`, `.chroma/`, `.graph/`) are derived from `knowledge/` files and can be rebuilt from scratch via `lithos reindex --clear`.

### 3.2 Knowledge File Format

Files use YAML frontmatter + Markdown body, compatible with Obsidian.

```markdown
---
id: <uuid>                        # Required: Unique identifier
title: <string>                   # Required: Document title
created_at: <ISO 8601 datetime>   # Required: Creation timestamp
updated_at: <ISO 8601 datetime>   # Required: Last update timestamp
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
source: <string>                  # Optional: Task ID or provenance note
supersedes: <uuid>                # Optional: ID of document this replaces
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
2. **Filename**: `[[note]]` → `*/note.md` (unresolved if ambiguous)
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

Stored in `.lithos/coordination.db`:

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
| `source_task` | string | No | Task ID or provenance note (stored as `source` in frontmatter) |

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
| `threshold` | float | No | Minimum similarity 0-1 (default: from config, 0.3) |
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
List all tags with document counts.

**Arguments:** None

**Returns:** `{ tags: { name: count, ... } }`

**Note:** To find documents with a specific tag, use `lithos_list(tags=["tag-name"])`.

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

**Returns:** `{ findings: [{ id, agent, summary, knowledge_id, created_at }] }`

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
5. Load coordination state from `.lithos/coordination.db`
6. Start file watcher

**Full rebuild** only when forced via `lithos reindex --clear`.

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

Stored in `.lithos/coordination.db` (SQLite, accessed via `aiosqlite` for async compatibility):

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

### 8.1 Configuration

Configuration is managed via `pydantic-settings` (`LithosConfig`). Values are resolved in order:

1. Defaults (hardcoded in `config.py`)
2. YAML config file (`config.yaml` in data directory or specified via `--config`)
3. Environment variables with `LITHOS_` prefix (e.g., `LITHOS_DATA_DIR`, `LITHOS_PORT`)

```yaml
# Server configuration
server:
  transport: stdio          # stdio | sse
  host: 0.0.0.0            # Bind address (all interfaces for Docker compatibility)
  port: 8765               # For SSE transport
  watch_files: true         # Enable file watcher for index updates

# Storage paths
storage:
  data_dir: ./data         # Base data directory
  knowledge_subdir: knowledge # Relative to data_dir

# Search configuration
search:
  embedding_model: all-MiniLM-L6-v2  # sentence-transformers model
  semantic_threshold: 0.3   # Default similarity threshold
  max_results: 50           # Maximum search results
  chunk_size: 500           # Target chunk size in characters
  chunk_max: 1000           # Maximum chunk size

# Coordination
coordination:
  claim_default_ttl_minutes: 60  # Default claim duration
  claim_max_ttl_minutes: 480     # Maximum claim duration

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
lithos serve --transport sse --host 0.0.0.0 --port 8765 --data-dir ./data

# Disable file watcher
lithos serve --no-watch --data-dir ./data

# Rebuild indices (incremental by default)
lithos reindex --data-dir ./data

# Clear and rebuild all indices from scratch
lithos reindex --data-dir ./data --clear

# Validate knowledge files
lithos validate --data-dir ./data
# Reports: broken [[wiki-links]], missing frontmatter, ambiguous links, stale references after renames

# Show knowledge base statistics
lithos stats --data-dir ./data

# Search knowledge base from CLI
lithos search "query text" --data-dir ./data
lithos search "query text" --semantic --data-dir ./data
```

---

## 9. Error Handling

### 9.1 Current Behavior

Tools indicate errors through their return values:

- **Boolean success fields**: Coordination tools return `{ success: false }` on failure (e.g., claim conflicts, missing tasks)
- **Empty results**: Search/read operations return empty results or `null` fields when items are not found
- **Exceptions**: Unexpected errors (file I/O, index corruption) propagate as MCP-level exceptions

### 9.2 Error Scenarios

| Scenario | Behavior |
|----------|----------|
| Knowledge item not found | `lithos_read` returns error, `lithos_delete` returns `{ success: false }` |
| Claim conflict (aspect taken) | `lithos_task_claim` returns `{ success: false }` |
| Claim renewal by wrong agent | `lithos_task_renew` returns `{ success: false }` |
| Invalid arguments | FastMCP validation rejects the call |
| Ambiguous wiki-link | Link treated as unresolved (no error raised) |

---

## 10. Future Considerations (Out of Scope for v0.1)

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
- Structured MCP error codes (`NOT_FOUND`, `CLAIM_CONFLICT`, `AMBIGUOUS_LINK`, etc.)
- Structured `source` provenance with `derived_from` links to source knowledge items
- `lithos_tags` filtering: accept a `tag` parameter to return documents with that tag
- `lithos_delete` audit trail logging (record which agent deleted what)

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

**Total: 20 MCP tools**

---

**End of Specification**
