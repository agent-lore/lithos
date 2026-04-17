# Lithos - Specification

Version: 0.1.5
Date: 2026-03-18
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

### 1.3 Compatibility Policy (Pre-1.0)

1. **MCP/API evolution is allowed**: Tool signatures and response envelopes may change to improve coherence.
2. **On-disk compatibility is required**: Existing Markdown/frontmatter knowledge must remain readable and valid.
3. **Migration safety over API stability**: When tradeoffs occur, preserve the knowledge corpus first.

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
│  │  ┌─────────────┐  ┌─────────────┐                          │  │
│  │  │   Agent     │  │  Event Bus  │                          │  │
│  │  │  Registry   │  │ (in-memory) │                          │  │
│  │  └─────────────┘  └─────────────┘                          │  │
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

1. **Write path**: Agent → MCP tool → Knowledge Manager → Write file → Update indices and graph cache → Emit event
2. **Read path**: Agent → MCP tool → Search Engine → Query indices → Return results
3. **Startup**: Ensure directories and coordination DB → Check rebuild conditions → Load graph cache or rebuild projections → Pre-warm embeddings in background → Ready

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
│   ├── coordination.db          # SQLite: tasks, claims, agents, findings
│   ├── edges.db                 # SQLite: LCMA typed edges (created lazily on first edge upsert)
│   └── stats.db                 # SQLite: LCMA retrieval stats, receipts, working memory (created lazily on first retrieve)
├── .tantivy/                    # Rebuildable index (full-text search)
├── .chroma/                     # Rebuildable index (semantic embeddings)
└── .graph/                      # Rebuildable cache (wiki-link graph)
```

**Authoritative vs. rebuildable:** `knowledge/` and `.lithos/` contain data that cannot be regenerated — they must be backed up and preserved. The index directories (`.tantivy/`, `.chroma/`, `.graph/`) are derived from `knowledge/` files and can be rebuilt from scratch via `lithos reindex --clear`.

**LCMA SQLite stores under `.lithos/`:**

- **`coordination.db`** — agents, tasks, claims, findings (pre-LCMA, unchanged).
- **`edges.db`** — LCMA typed/weighted edges. Tables: `edges` (`edge_id`, `from_id`, `to_id`, `type`, `weight`, `namespace`, `created_at`, `updated_at`, `provenance_actor`, `provenance_type`, `evidence`, `conflict_state`). Created lazily on the first `lithos_edge_upsert` call. Separate from the `.graph/` NetworkX wiki-link cache — `edges.db` carries semantic/learned relationships, NetworkX continues to power `lithos_links`.
- **`stats.db`** — LCMA retrieval state. Tables: `node_stats`, `coactivation`, `enrich_queue`, `working_memory`, `receipts`. Created lazily on the first `lithos_retrieve` call (when the first receipt is written). The `enrich_queue` and reinforcement columns in `node_stats` are placeholders for the MVP 2 `lithos-enrich` worker.

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
source_url: <string>              # Optional: Canonical URL provenance (normalized on write)
derived_from_ids:                 # Optional: Declared lineage (list of UUIDs)
  - <uuid-1>
  - <uuid-2>
expires_at: <ISO 8601 datetime>   # Optional: Freshness deadline (UTC); null = never expires
supersedes: <uuid>                # Optional: ID of document this replaces
version: <int>                    # Managed by Lithos; starts at 1 and increments on update
# --- LCMA fields (additive, optional, with defaults applied at read time) ---
schema_version: <int>             # Optional: LCMA schema version (default: 1)
namespace: <string>               # Optional: LCMA namespace. When absent, derived from path:
                                  #   knowledge/foo.md         → "default"
                                  #   knowledge/shared/foo.md  → "shared"
                                  #   knowledge/project/x/y.md → "project/x"
                                  # An explicit value overrides path derivation and is
                                  # persisted only when passed to lithos_write.
access_scope: <enum>              # Optional: shared | task | agent_private (default: shared)
                                  # Advisory visibility — not a security control. `task`
                                  # requires source_task; `agent_private` filters by author.
note_type: <enum>                 # Optional: observation | agent_finding | summary |
                                  #           concept | task_record | hypothesis
                                  # (default: observation)
status: <enum>                    # Optional: active | archived | quarantined (default: active)
summaries:                        # Optional: nested object with short/long summaries
  short: <string>                 # Optional, agent-written
  long: <string>                  # Optional, agent-written
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

Normative contract references for the write path:

- `docs/plans/unified-write-contract.md`
- `docs/plans/final-architecture-guardrails.md`
- `docs/plans/target-search-schema.md` (search projection schema registry)

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
| `source_url` | string | No | Canonical URL provenance (http/https), dedup key after normalization. Pass `""` to clear on update. |
| `derived_from_ids` | string[] | No | Canonical declared lineage (UUIDs). On create: `null`/omit stores `[]`. On update: `null`/omit preserves existing, `[]` clears, non-empty replaces. Self-references rejected. |
| `ttl_hours` | float | No | Relative freshness window; converted to `expires_at` |
| `expires_at` | string | No | Absolute ISO datetime freshness deadline |
| `expected_version` | int | No | Optimistic-locking guard for updates; ignored on create |
| `schema_version` | int | No | LCMA schema version (default: 1 on create). Preserved on update if omitted. |
| `namespace` | string | No | LCMA namespace. Persisted only when explicitly passed; derived from path at read time otherwise. |
| `access_scope` | enum | No | `shared` \| `task` \| `agent_private` (default: `shared` on create). `task` requires `source_task`. |
| `note_type` | enum | No | `observation` \| `agent_finding` \| `summary` \| `concept` \| `task_record` \| `hypothesis` (default: `observation` on create). |
| `status` | enum | No | `active` \| `archived` \| `quarantined` (default: `active` on create). |
| `summaries` | object | No | Nested `{short, long}` object. Both keys optional, both must be strings if present. |

**Returns (status envelope):**

`{ status: "created", id: string, path: string, version: int, warnings: string[] }`

`{ status: "updated", id: string, path: string, version: int, warnings: string[] }`

`{ status: "duplicate", duplicate_of: { id, title, source_url }, message: string, warnings: string[] }`

`{ status: "error", code: "invalid_input", message: string, warnings: [] }`

`{ status: "error", code: "content_too_large", message: string, warnings: [] }`

`{ status: "error", code: "slug_collision", message: string, warnings: [] }`

`{ status: "error", code: "version_conflict", message: string, current_version: int, warnings: [] }`

**Behavior on update:** If `id` is provided and exists, the agent is added to `contributors` if not already present.

**Update semantics:** Omitted optional fields preserve existing values. Some fields support explicit clear. At the MCP boundary, FastMCP cannot distinguish omitted from `null`, so clearable string fields use `""` (empty string) as the clear signal (e.g., `source_url: ""`). See `unified-write-contract.md` for the full MCP boundary convention.

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

**Metadata includes:** `derived_from_ids` (list of source UUIDs, may be empty).

**Truncation behavior:** When `max_length` is specified, content is truncated at the nearest paragraph or sentence boundary at or before the limit. Returns `truncated: true` if content was shortened.

#### `lithos_delete`
Delete a knowledge file.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `id` | string | Yes | UUID of knowledge item to delete |
| `agent` | string | Yes | Agent performing deletion (required for audit trail and auto-registration) |

**Returns:** `{ success: true }` on success, or `{ status: "error", code: "doc_not_found", message: string }` if the document does not exist.

#### `lithos_search`
Unified search across the knowledge base.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `query` | string | Yes | Search query |
| `limit` | int | No | Max results (default: 10) |
| `mode` | string | No | `"hybrid"` \| `"fulltext"` \| `"semantic"` (default: `"hybrid"`) |
| `tags` | string[] | No | Filter by tags (AND) |
| `author` | string | No | Filter by author |
| `path_prefix` | string | No | Filter by path prefix |
| `threshold` | float | No | Minimum similarity 0-1 for semantic/hybrid (default: 0.5) |

**Returns:** `{ results: [{ id, title, snippet, score, path, source_url, updated_at, is_stale, derived_from_ids }] }`

**Modes:**
- `hybrid`: Reciprocal Rank Fusion over full-text and semantic results
- `fulltext`: Tantivy BM25 search
- `semantic`: ChromaDB vector similarity search

**Notes:**
- Search operates on chunks internally but returns deduplicated documents.
- In `semantic` mode, the returned `score` is the semantic similarity value.
- Invalid `mode` returns `{ status: "error", code: "invalid_mode", message }`.

#### `lithos_cache_lookup`
Check the knowledge base for a cached answer before performing expensive research.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `query` | string | Yes | Natural language query for semantic matching |
| `source_url` | string | No | Exact URL to check first (fast path) |
| `max_age_hours` | float | No | Reject documents older than N hours (by `updated_at`) |
| `min_confidence` | float | No | Minimum confidence score (default: 0.5) |
| `limit` | int | No | Max candidates to evaluate (default: 3) |
| `tags` | string[] | No | Filter by tags |

**Returns (hit):**
```json
{
  "hit": true,
  "document": { "id": "...", "title": "...", "content": "...", "source_url": "...", "confidence": 0.9, "updated_at": "...", "expires_at": "...", "tags": ["..."] },
  "stale_exists": false,
  "stale_id": null
}
```

**Returns (miss with stale candidate):**
```json
{
  "hit": false,
  "document": null,
  "stale_exists": true,
  "stale_id": "<uuid>"
}
```

**Returns (clean miss):**
```json
{
  "hit": false,
  "document": null,
  "stale_exists": false,
  "stale_id": null
}
```

**Returns (error):**
```json
{ "status": "error", "code": "invalid_input", "message": "..." }
```

```json
{ "status": "error", "code": "search_backend_error", "message": "..." }
```

**Evaluation pipeline:**
1. **Fast path**: If `source_url` provided, exact URL lookup via `find_by_source_url()`, filtered by tags.
2. **Semantic fallback**: If fast path misses, `semantic_search(threshold=0.0)` returns top candidates.
3. **Candidate evaluation** (in order): confidence filter → staleness check (`expires_at`) → `max_age_hours` check → first passing candidate = hit.
4. **Stale tracking**: If all candidates fail due to staleness, returns `stale_id` so the caller can update the stale document.

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
| `title_contains` | string | No | Case-insensitive substring match on title |
| `content_query` | string | No | Tantivy full-text query applied after the base filters |

**Returns:** `{ items: [{ id, title, path, updated, tags, source_url, derived_from_ids }], total: int }`

**Behavior:**
- When `title_contains` or `content_query` is present, Lithos fetches the full base-filtered set first, then applies post-filters before pagination. This keeps `items` and `total` correct across pages.
- `content_query` backend failures return `{ status: "error", code: "search_backend_error", message }`.

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

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `prefix` | string | No | Case-insensitive prefix filter on tag names |

**Returns:** `{ tags: { name: count, ... } }`

**Note:** To find documents with a specific tag, use `lithos_list(tags=["tag-name"])`.

#### `lithos_provenance`
Traverse provenance lineage (derived-from relationships) for a knowledge item.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `id` | string | Yes | UUID of knowledge item |
| `direction` | string | No | "sources", "derived", or "both" (default: "both") |
| `depth` | int | No | Traversal depth 1-3 (default: 1) |
| `include_unresolved` | bool | No | Include unresolved source IDs (default: true) |

**Returns:**
```json
{
  "id": "<queried-uuid>",
  "sources": [{ "id": "<uuid>", "title": "<string>" }],
  "derived": [{ "id": "<uuid>", "title": "<string>" }],
  "unresolved_sources": ["<uuid>", ...]
}
```

**Behavior:**
- Uses BFS traversal over in-memory provenance indexes.
- `sources` walks the `derived_from_ids` chain (what was this derived from?).
- `derived` walks the reverse index (what was derived from this?).
- `depth` is clamped to 1-3. Depth > 1 follows multi-hop chains (e.g., A→B→C at depth 2).
- Cycles are handled via a visited set — BFS terminates without infinite loops.
- `unresolved_sources` is only populated when `include_unresolved=true` (the default). Contains source UUIDs that reference documents not currently in the knowledge base.
- Returns `{ status: "error", code: "doc_not_found" }` for unknown IDs.
- Results are sorted by ID for deterministic output.

#### `lithos_related`

Composite "what is this document related to?" view. Merges wiki-link navigation, derived-from provenance, and typed LCMA edges into a single response so agents don't have to fan out across three tools and mentally join the results.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `id` | string | Yes | UUID of knowledge item |
| `include` | list[string] | No | Subset of `["links", "provenance", "edges"]` to populate (default: all three) |
| `depth` | int | No | BFS depth 1-3 for `links` and `provenance` (default: 1). Ignored by `edges`. |
| `namespace` | string | No | Optional namespace filter applied to `edges` only |

**Returns:**
```json
{
  "id": "<queried-uuid>",
  "included": ["links", "provenance", "edges"],
  "links": {
    "outgoing": [{ "id": "<uuid>", "title": "<string>" }],
    "incoming": [{ "id": "<uuid>", "title": "<string>" }]
  },
  "provenance": {
    "sources": [{ "id": "<uuid>", "title": "<string>" }],
    "derived": [{ "id": "<uuid>", "title": "<string>" }],
    "unresolved_sources": ["<uuid>", ...]
  },
  "edges": {
    "outgoing": [<edge-record>, ...],
    "incoming": [<edge-record>, ...]
  },
  "related_ids": ["<uuid>", ...]
}
```

**Behavior:**
- Sections not listed in `include` are omitted entirely (not emitted as empty keys).
- Unknown `include` values are silently ignored so forward-compatible callers don't break when new backends land.
- `edges` section is empty when LCMA is disabled in config.
- `related_ids` is the deduped, sorted union of every id referenced across the included sections. The queried document's own id is excluded so callers can iterate without filtering.
- `lithos_links`, `lithos_provenance`, and `lithos_edge_list` remain available for scenarios that need finer-grained control (single direction, edge-type filter, etc.). This tool is for the common case.
- Returns `{ status: "error", code: "doc_not_found" }` for unknown IDs.

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

#### `lithos_task_update`
Update mutable task metadata.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Task ID |
| `agent` | string | Yes | Agent making the update |
| `title` | string | No | Replacement title |
| `description` | string | No | Replacement description |
| `tags` | string[] | No | Replacement tags |

**Returns:** `{ success: true, message }` on success, or `{ status: "error", code, message }` on failure (codes: `invalid_input`, `task_not_found`).

**Behavior:**
- At least one of `title`, `description`, or `tags` must be provided.
- Only `open` tasks can be updated. Completed and cancelled tasks are treated as not found at the MCP boundary.

#### `lithos_task_claim`
Claim an aspect of a task.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Task ID |
| `aspect` | string | Yes | What aspect you're working on |
| `agent` | string | Yes | Your agent identifier |
| `ttl_minutes` | int | No | Claim duration (default: 60, max: 480) |

**Returns:** `{ success: true, expires_at: string }` on success, or `{ status: "error", code: "claim_failed", message }` on failure.

#### `lithos_task_renew`
Extend an existing task claim.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Task ID |
| `aspect` | string | Yes | The aspect claim to renew |
| `agent` | string | Yes | Your agent identifier |
| `ttl_minutes` | int | No | New duration from now (default: 60, max: 480) |

**Returns:** `{ success: true, new_expires_at: string }` on success, or `{ status: "error", code: "claim_not_found", message }` on failure.

**Note:** Only the agent holding the claim can renew it.

#### `lithos_task_release`
Release a task claim.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Task ID |
| `aspect` | string | Yes | The aspect claim to release |
| `agent` | string | Yes | Your agent identifier |

**Returns:** `{ success: true }` on success, or `{ status: "error", code: "claim_not_found", message }` if no matching claim exists.

#### `lithos_task_complete`
Mark a task as completed.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Task ID |
| `agent` | string | Yes | Agent marking completion |

**Returns:** `{ success: true }` on success, or `{ status: "error", code: "task_not_found", message }` if the task does not exist or is not open.

**Behavior:** Sets task status to 'completed' and releases all active claims on the task.

#### `lithos_task_cancel`
Cancel a task and release all claims.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Task ID |
| `agent` | string | Yes | Agent cancelling the task |
| `reason` | string | No | Optional cancellation reason |

**Returns:** `{ success: true }` on success, or `{ status: "error", code: "task_not_found", message }` on failure.

**Behavior:** Marks an open task as `cancelled` and deletes all claims on that task. The optional `reason` is accepted by the MCP surface but is not persisted in SQLite.

#### `lithos_task_list`
List tasks with optional filters.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `agent` | string | No | Filter by creating agent |
| `status` | string | No | Filter by task status: `open`, `completed`, or `cancelled` |
| `tags` | string[] | No | Filter to tasks containing all listed tags |
| `since` | string | No | Filter by `created_at >= since` (ISO datetime) |

**Returns:** `{ tasks: [{ id, title, description, status, created_by, created_at, tags }] }`

#### `lithos_task_status`
Get task status and claims.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Specific task ID |

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
  "tags": 89,
  "duplicate_urls": 0
}
```

**Use case:** Allows agents to understand knowledge base scale before issuing broad queries.

### 5.6 LCMA Operations (Phase 7 MVP 1)

These tools are additive to the pre-LCMA surface — they do not replace `lithos_search`, `lithos_read`, or `lithos_links`. See `docs/plans/lcma-design.md` for the design rationale.

#### `lithos_retrieve`

PTS-style retrieval orchestrating seven scouts in parallel and reranking via a fast Terrace 1 pass. Returns `lithos_search`-compatible results plus LCMA-only audit metadata.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `query` | string | Yes | Free-text query. |
| `limit` | int | No | Max results (default: 10). |
| `namespace_filter` | string[] | No | Restrict candidates to these LCMA namespaces. Honors explicit `namespace` overrides in frontmatter. |
| `agent_id` | string | No | Caller agent ID; used for `agent_private` access scope gating. |
| `task_id` | string | No | When set, enables `scout_task_context`, `task`-scope gating, and per-`(task_id, node_id)` working-memory upserts. |
| `surface_conflicts` | bool | No | Recorded in the receipt (default: `false`). Contradiction surfacing activates in MVP 2. |
| `max_context_nodes` | int | No | Phase B seed size (default: `limit`). |
| `tags` | string[] | No | Global tag filter applied to **every** scout. |
| `path_prefix` | string | No | Global path-prefix filter applied to **every** scout. |

**Returns:**

```json
{
  "results": [
    {
      "id": "...",
      "title": "...",
      "snippet": "...",
      "score": 0.42,
      "path": "shared/note.md",
      "source_url": "",
      "updated_at": "2026-03-18T12:00:00+00:00",
      "is_stale": false,
      "derived_from_ids": [],
      "reasons": ["lexical match score 0.91"],
      "scouts": ["scout_lexical"],
      "salience": 0.42
    }
  ],
  "temperature": 0.5,
  "terrace_reached": 1,
  "receipt_id": "rcpt_<short-uuid>"
}
```

The `id`/`title`/`snippet`/`score`/`path`/`source_url`/`updated_at`/`is_stale`/`derived_from_ids` keys mirror the `lithos_search` shape so clients that read only those fields work unchanged. `reasons`/`scouts`/`salience` are LCMA-only additive fields.

**Receipt audit trail:** every call writes a row to `stats.db.receipts` with columns including `id`, `ts`, `query`, `namespace_filter`, `scouts_fired` (canonical names of every scout that ran cleanly — empty results still count as fired), `candidates_considered`, `final_nodes` (JSON array of `{id, reasons, scouts}` objects), `surface_conflicts`, `temperature`, and `terrace_reached`.

**Working memory:** when `task_id` is set, each result is upserted into `stats.db.working_memory` keyed on `(task_id, node_id)`, incrementing `activation_count` and tracking `first_seen_at` / `last_seen_at`.

**MVP 1 limits:** `temperature` always returns `LcmaConfig.temperature_default` (0.5). Coherence-based computation activates in MVP 3 once `edges.db` carries enough typed edges.

#### `lithos_edge_upsert`

Insert or update a typed weighted edge in `edges.db`. The unique key is `(from_id, to_id, type, namespace)`.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `from_id` | string | Yes | Source node UUID. |
| `to_id` | string | Yes | Target node UUID. |
| `type` | string | Yes | Edge type (e.g. `related_to`, `supports`, `contradicts`, `derived_from`). |
| `weight` | float | Yes | Edge weight 0..1. |
| `namespace` | string | Yes | LCMA namespace this edge belongs to. |
| `provenance_actor` | string | No | Agent or rule ID that authored the edge. |
| `provenance_type` | string | No | `human` \| `agent` \| `rule` \| `frontmatter`. |
| `evidence` | object \| string | No | Anchors/snippets supporting the edge. |
| `conflict_state` | string | No | Reserved for `contradicts` edges (MVP 2). |

**Returns:** `{ "status": "ok", "edge_id": "edge_<short-uuid>" }`.

**Side effect:** emits an `edge.upserted` event on the in-memory event bus (see §8.2).

#### `lithos_edge_list`

Query edges by node, type, or namespace.

**Arguments:** `from_id`, `to_id`, `type`, `namespace` — all optional. Filters compose as `AND`.

**Returns:** `{ "results": [ { edge_id, from_id, to_id, type, weight, namespace, created_at, updated_at, provenance_actor, provenance_type, evidence, conflict_state } ] }`.

### 5.7 HTTP Endpoints

These endpoints are standard HTTP routes mounted alongside the MCP transport. They are **not** MCP tools and do not appear in `tools/list`.

#### `GET /health`

Lightweight health check for Docker `HEALTHCHECK`, load balancers, and monitoring.

**Returns (200 OK):**
```json
{
  "status": "ok",
  "timestamp": "2026-03-18T12:00:00+00:00",
  "components": {
    "kb_directory": { "status": "ok" },
    "embedding_model": { "status": "ok" },
    "knowledge_base": { "status": "ok" }
  }
}
```

**Returns (503 Service Unavailable):** Same shape with `"status": "degraded"` and per-component `error` strings for any unhealthy component.

**Components checked:**

| Component | Check |
|-----------|-------|
| `kb_directory` | Knowledge base directory exists on disk |
| `embedding_model` | ChromaDB embedding health check passes |
| `knowledge_base` | Can list at least one document |

**Use case:** The Docker image uses this endpoint in its `HEALTHCHECK` directive (`curl -f http://localhost:8765/health`). External orchestrators and load balancers can poll it to determine readiness.

---

## 6. Index Behavior

### 6.1 Startup

1. Ensure data directories exist
2. Initialize coordination database (`.lithos/coordination.db`)
3. Check if Tantivy index needs rebuild (schema version mismatch)
4. **Rebuild decision** (first matching condition wins):
   - `rebuild_on_start` config flag is set → full rebuild
   - Tantivy schema version requires rebuild → full rebuild
   - NetworkX graph cache (`.graph/graph.json`) fails to load → full rebuild
   - NetworkX graph cache `GRAPH_CACHE_VERSION` field does not match the expected version → silent rebuild with a warning log
   - Graph cache loads successfully → use existing indices
5. **Full rebuild** (when triggered): clear all indices, scan `knowledge/` directory, re-parse and re-index every `.md` file
6. Pre-warm the embedding model in the background

**File watcher startup:** The server supports watching, but the watcher is started by the CLI `serve` command when `--watch` is enabled. `initialize()` does not start it by itself.

**On-demand rebuild** via `lithos reindex --clear`.

### 6.2 File Change Handling

| Event | Action |
|-------|--------|
| File created | Parse, chunk, add to all indices |
| File modified | Parse, re-chunk, update all indices |
| File deleted | Remove from all indices |
| File moved/renamed | No dedicated move handler; watchdog create/modify/delete events are processed best-effort |

**Note on renames and wiki-links:** When a file is renamed, UUID matching preserves identity in indices. However, wiki-link text in *other* files still points to the old path. `lithos validate` reports these as broken links.

### 6.3 Index Persistence

- **Tantivy**: Persisted to `.tantivy/` directory
- **ChromaDB**: Persisted to `.chroma/` directory  
- **NetworkX**: Cached to `.graph/graph.json`, rebuilt if missing

**Graph cache format:** The JSON cache includes a `GRAPH_CACHE_VERSION` field alongside the serialised graph, node maps, and alias tables. If the version field in the file does not match the expected constant in the codebase, Lithos performs an automatic silent rebuild and logs a warning (version mismatch is not an error).

### 6.4 Reconcile / Repair

Lithos provides an operator-facing reconcile path that repairs derived state without mutating authoritative markdown.

**Available scopes:**

- `indices`: detect drift between markdown corpus and Tantivy/Chroma projections and rebuild affected backends
- `graph`: detect graph cache drift and rebuild `.graph/graph.json`
- `provenance_projection`: project `derived_from_ids` from frontmatter into the LCMA `edges.db` as `type='derived_from'` edges, and remove orphan projections that no longer match any document. Returns `supported=false` (and is a no-op) when `edges.db` does not exist on disk. When `edges.db` exists, the action payload reports `{created: N, removed: M}`.
- `all`: runs the scopes above in order and aggregates status

**Operational behavior:**

- `dry_run=true` computes the planned actions using the same diff logic as a real run, but applies no writes. For `provenance_projection`, dry-run reports the same `{created, removed}` counts the live run would have applied.
- authoritative markdown/frontmatter is never rewritten
- repeated runs are expected to be idempotent when no drift exists
- the CLI command is `lithos reconcile`

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

## 8. Event System

Lithos includes an in-memory event bus that emits `LithosEvent` on all write, delete, task, finding, and agent-register success paths, as well as from the file watcher. This internal bus also backs a best-effort SSE delivery surface at `GET /events`. There are still no event MCP tools and no webhook delivery surface.

### 8.1 LithosEvent Schema

| Field | Type | Description |
|-------|------|-------------|
| `id` | string (UUID) | Auto-generated unique event identifier; stable dedup key |
| `type` | string | Event type constant (see table below) |
| `timestamp` | datetime (UTC) | Defaults to `datetime.now(UTC)` |
| `agent` | string | Agent that triggered the event (empty string if unknown, e.g. file watcher) |
| `payload` | dict | Event-specific key-value data |
| `tags` | list[str] | Tags from the affected entity (e.g. document tags for note events; empty for non-note events) |

### 8.2 Event Types

| Type Constant | Emitted By | Payload Fields |
|---------------|-----------|----------------|
| `note.created` | `lithos_write` (create) | `id`, `title`, `path` |
| `note.updated` | `lithos_write` (update), file watcher (create/modify) | `id`, `title`, `path` (tool); `path` (watcher) |
| `note.deleted` | `lithos_delete`, file watcher (delete) | `id`, `path` (tool); `path` (watcher) |
| `task.created` | `lithos_task_create` | `task_id`, `title` |
| `task.claimed` | `lithos_task_claim` | `task_id`, `agent`, `aspect` |
| `task.released` | `lithos_task_release` | `task_id`, `agent`, `aspect` |
| `task.completed` | `lithos_task_complete` | `task_id`, `agent` |
| `task.cancelled` | `lithos_task_cancel` | `task_id`, `agent`, `reason` |
| `finding.posted` | `lithos_finding_post` | `finding_id`, `task_id`, `agent` |
| `agent.registered` | `lithos_agent_register` | `agent_id`, `name` |
| `batch.queued` | Defined constant only; not currently emitted by server tool paths | — |
| `batch.applying` | Defined constant only; not currently emitted by server tool paths | — |
| `batch.projecting` | Defined constant only; not currently emitted by server tool paths | — |
| `batch.completed` | Defined constant only; not currently emitted by server tool paths | — |
| `batch.failed` | Defined constant only; not currently emitted by server tool paths | — |

### 8.3 Emission Points

- **Tool handlers**: Events are emitted after the operation succeeds but before returning to the caller. Each emission is wrapped in `try/except` so event bus failures never propagate to the caller.
- **File watcher**: `handle_file_change` emits `note.updated` on file create/modify and `note.deleted` on file delete. The watchdog observer runs on OS threads; `asyncio.run_coroutine_threadsafe` bridges to the event loop. Emission failures never crash the file watcher.
- **No-event cases**: `lithos_write` with `status=duplicate` or `status=invalid_input` emits no event. Failed `lithos_delete` (item not found) emits no event.

### 8.4 Subscriber Semantics

- **Subscribe**: `EventBus.subscribe(event_types=None, tags=None)` returns a bounded `asyncio.Queue`. Optional filters match by event type list and/or tag list.
- **Unsubscribe**: `EventBus.unsubscribe(queue)` removes the subscriber.
- **Backpressure**: If a subscriber queue is full, the event is dropped for that subscriber and a per-subscriber drop counter is incremented (`get_drop_count(queue)`).
- **Disabled mode**: When `EventsConfig.enabled=False`, `emit()` is a no-op — no fan-out, no buffer append.

### 8.5 Best-Effort Contract

- `emit()` never raises — all exceptions are caught and logged.
- Emission failures are isolated: a broken subscriber cannot affect other subscribers or the underlying operation.
- Events are delivered in process-local best-effort order for sequential same-loop emits.
- `event.id` (UUID) serves as a stable dedup key for consumers.

### 8.6 Ring Buffer

The event bus maintains an in-memory ring buffer of the last N events using `collections.deque(maxlen=N)`. SSE replay uses `get_buffered_since(event_id)` to replay buffered events after a known event ID. Buffer size is configurable via `events.event_buffer_size` (default: 500).

### 8.7 SSE Delivery Surface

Lithos exposes a best-effort Server-Sent Events endpoint at `GET /events`.

**Query parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `types` | string | No | Comma-separated event type filter |
| `tags` | string | No | Comma-separated tag filter; any matching tag passes |
| `since` | string | No | Replay buffered events strictly after the given event ID |

**Headers:**

| Name | Description |
|------|-------------|
| `Last-Event-ID` | Standard SSE reconnect header; takes precedence over `since` |

**Behavior:**

- Returns `text/event-stream`.
- Replays buffered events first when `since` or `Last-Event-ID` is supplied, then streams live events.
- Emits periodic keepalive comments when idle.
- Returns `503` when `events.sse_enabled=false`.
- Returns `429` when `events.max_sse_clients` is exceeded.
- When MCP auth is configured, `/events` uses the same auth boundary and returns `401` for unauthenticated requests.
- Delivery is best-effort and process-local; missed events outside the in-memory ring buffer cannot be replayed.

---

## 9. Configuration

### 9.1 Configuration

Configuration is managed via `pydantic-settings` (`LithosConfig`). Values are resolved in order:

1. Defaults (hardcoded in `config.py`)
2. YAML config file specified via `--config`
3. Environment variables with `LITHOS_` prefix (e.g., `LITHOS_DATA_DIR`, `LITHOS_PORT`)

```yaml
# Server configuration
server:
  transport: stdio          # stdio | sse
  host: 127.0.0.1          # Default bind address
  port: 8765               # For SSE transport
  watch_files: true         # Enable file watcher for index updates

# Storage paths
storage:
  data_dir: ./data         # Base data directory
  knowledge_subdir: knowledge # Relative to data_dir
  max_content_size_bytes: 1000000 # Reject larger MCP writes

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

# Telemetry
# Metrics, traces, and logs are exported via OpenTelemetry OTLP/HTTP push to a
# configured collector. There is no /metrics scrape endpoint on the Lithos
# process itself — the observability stack in `lithos-observability/` runs a
# collector whose Prometheus exporter (on :8889) is what Prometheus scrapes.
# See README → "Telemetry & Observability" for the full data-flow diagram.
telemetry:
  enabled: false
  endpoint: null           # OTLP base URL, e.g. http://otel-collector:4318
  console_fallback: false  # print spans/metrics to stdout when no endpoint
  service_name: lithos
  environment: null        # OTEL deployment.environment
  export_interval_ms: 30000

# Event Bus
events:
  enabled: true              # Enable/disable event bus (no-op when false)
  event_buffer_size: 500     # Ring buffer capacity (last N events)
  subscriber_queue_size: 100 # Max queued events per subscriber
  sse_enabled: true          # Enable/disable GET /events SSE delivery
  max_sse_clients: 50        # Max concurrent SSE clients
```

### 9.2 Command Line Interface

```bash
# Run with stdio transport (for MCP)
lithos --data-dir ./data serve --transport stdio

# Run with SSE transport (for HTTP access)
lithos --data-dir ./data serve --transport sse --host 0.0.0.0 --port 8765

# Disable file watcher
lithos --data-dir ./data serve --no-watch

# Route OTEL metrics + spans to stdout (local debugging without a collector)
lithos --data-dir ./data serve --telemetry-console

# Rebuild indices (incremental by default)
lithos --data-dir ./data reindex

# Clear and rebuild all indices from scratch
lithos --data-dir ./data reindex --clear

# Validate knowledge files
lithos --data-dir ./data validate
# Reports: broken [[wiki-links]], missing frontmatter, ambiguous links, stale references after renames

# Reconcile derived state without touching markdown
lithos --data-dir ./data reconcile
lithos --data-dir ./data reconcile --scope graph --dry-run --json-output

# Show knowledge base statistics
lithos --data-dir ./data stats

# Search knowledge base from CLI
lithos --data-dir ./data search "query text"
lithos --data-dir ./data search "query text" --semantic

# Inspect backends, agents, tasks, or a document
lithos --data-dir ./data inspect health
lithos --data-dir ./data inspect agents
lithos --data-dir ./data inspect tasks --all
lithos --data-dir ./data inspect doc <id-or-path> --content
```

---

## 10. Error Handling

### 10.1 Current Behavior

Tools indicate routine domain failures through return values, and unexpected backend failures may still surface as MCP-level exceptions.

- **Structured status envelopes**: `lithos_write` returns `{ status: "error", code, message, ... }` for invalid input and contract-level failures
- **Structured error envelopes on many tools**: `lithos_delete`, `lithos_task_claim`, `lithos_task_renew`, `lithos_task_release`, `lithos_task_complete`, `lithos_task_update`, `lithos_task_cancel`, `lithos_search`, `lithos_list`, and `lithos_cache_lookup` use `{ status: "error", code, message }` for routine domain failures
- **Nullable results**: `lithos_agent_info` returns `null` when the agent is not found
- **Exceptions**: Unexpected file/index/backend errors may still propagate at the MCP layer

### 10.2 Error Scenarios

| Scenario | Behavior |
|----------|----------|
| Knowledge item not found | `lithos_read` returns `{ status: "error", code: "doc_not_found" }`; `lithos_delete` returns the same envelope |
| Unknown search mode | `lithos_search` returns `{ status: "error", code: "invalid_mode" }` |
| Search backend failure during `lithos_list(content_query=...)` | `lithos_list` returns `{ status: "error", code: "search_backend_error" }` |
| Search backend failure during cache lookup fallback | `lithos_cache_lookup` returns `{ status: "error", code: "search_backend_error" }` |
| Claim conflict (aspect taken / task closed / task missing) | `lithos_task_claim` returns `{ status: "error", code: "claim_failed" }` |
| Claim renewal by wrong agent or missing claim | `lithos_task_renew` returns `{ status: "error", code: "claim_not_found" }` |
| Claim release with no matching claim | `lithos_task_release` returns `{ status: "error", code: "claim_not_found" }` |
| Completing unknown or non-open task | `lithos_task_complete` returns `{ status: "error", code: "task_not_found" }` |
| Updating task with no fields provided | `lithos_task_update` returns `{ status: "error", code: "invalid_input" }` |
| Updating or cancelling unknown/closed task | `lithos_task_update` / `lithos_task_cancel` returns `{ status: "error", code: "task_not_found" }` |
| Invalid arguments | FastMCP validation rejects the call |
| Ambiguous wiki-link | Link treated as unresolved (no error raised) |
| Write content exceeds configured limit | `lithos_write` returns `{ status: "error", code: "content_too_large" }` |
| Slug collision on create/update | `lithos_write` returns `{ status: "error", code: "slug_collision" }` |
| Optimistic lock mismatch | `lithos_write` returns `{ status: "error", code: "version_conflict", current_version }` |

---

## 11. Future Considerations (Out of Scope for v0.1)

These are explicitly not part of the initial implementation but may be considered later:

- Web UI for browsing knowledge
- Agent Zero memory sync/bridge
- Knowledge versioning (beyond git)
- Multi-node deployment
- ~~Access control / namespaces~~ (LCMA MVP 1 introduces advisory `namespace` and `access_scope` frontmatter fields enforced inside `lithos_retrieve`'s scouts. Legacy `lithos_search`/`lithos_read`/`lithos_list` remain unrestricted — caller-context-aware enforcement on those tools is deferred. Not a security control.)
- ~~Knowledge expiration / TTL~~ (Implemented in Phase 4 via `expires_at`, `ttl_hours`, `lithos_cache_lookup`, and `is_stale` in search results)
- Automated knowledge quality scoring
- Contradictory knowledge resolution
- Integration with external knowledge sources
- Full edit history / provenance log
- Hierarchical multi-hop link results
- ~~Structured MCP error codes (`NOT_FOUND`, `CLAIM_CONFLICT`, `AMBIGUOUS_LINK`, etc.)~~ (Implemented via `{ status: "error", code, message }` envelopes; see §10.2 for the full list of error codes)
- ~~Structured `source` provenance with `derived_from` links to source knowledge items~~ (Implemented in Phase 3 via `derived_from_ids` and `lithos_provenance`)
- ~~`lithos_tags` prefix filtering~~ (Implemented via `prefix`)
- `lithos_delete` audit trail logging (record which agent deleted what)

---

## Appendix A: Example Session

```
# Check knowledge base stats
→ lithos_stats()
← { documents: 0, chunks: 0, agents: 0, active_tasks: 0, open_claims: 0, tags: 0, duplicate_urls: 0 }

# Agent Zero registers (optional, would auto-register anyway)
→ lithos_agent_register(id="agent-zero", name="Agent Zero", type="agent-zero")
← { success: true, created: true }

# Agent Zero stores a discovery
→ lithos_write(title="Python asyncio.gather patterns", content="...", tags=["python", "async"], agent="agent-zero")
← { status: "created", id: "abc-123", path: "python-asyncio-gather-patterns.md", version: 1, warnings: [] }

# OpenClaw searches for async knowledge (semantic search uses chunks internally)
→ lithos_search(query="how to run async tasks concurrently in python", mode="semantic")
← { results: [{ id: "abc-123", title: "Python asyncio.gather patterns", score: 0.89, snippet: "...best matching chunk..." }] }

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
← { documents: 1, chunks: 3, agents: 2, active_tasks: 0, open_claims: 0, tags: 2, duplicate_urls: 0 }
```

---

## Appendix B: Tool Summary

| Category | Tools |
|----------|-------|
| Knowledge | `lithos_write`, `lithos_read`, `lithos_delete`, `lithos_search`, `lithos_list`, `lithos_cache_lookup` |
| Graph | `lithos_links`, `lithos_tags`, `lithos_provenance`, `lithos_related` |
| Agent | `lithos_agent_register`, `lithos_agent_info`, `lithos_agent_list` |
| Coordination | `lithos_task_create`, `lithos_task_update`, `lithos_task_claim`, `lithos_task_renew`, `lithos_task_release`, `lithos_task_complete`, `lithos_task_cancel`, `lithos_task_list`, `lithos_task_status`, `lithos_finding_post`, `lithos_finding_list` |
| System | `lithos_stats` |
| LCMA (Phase 7 MVP 1) | `lithos_retrieve`, `lithos_edge_upsert`, `lithos_edge_list` |
| HTTP | `GET /health` (not an MCP tool; see §5.7) |

**Total: 27 MCP tools + 1 HTTP endpoint** (24 pre-LCMA + 3 LCMA MVP 1 additive)

---

**End of Specification**
