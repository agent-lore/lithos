# Lithos - Specification

Version: 0.3.0
Date: 2026-06-04
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

The runtime is organised around the corpus/derived-view distinction defined in
[`CONTEXT.md`](../CONTEXT.md). The **Corpus** is the joint source of truth
(Markdown notes plus agent-asserted edges in `edges.db`). Three derived views
project from the notes tier — `SearchEngine`, the wiki-link graph, and
`ProvenanceProjection` — and the agent-facing `CognitiveMemory` module reads
through their public surfaces while owning its own accumulated stats.

```
┌─────────────────────────────────────────────────────────────────────┐
│                              Lithos                                 │
│                                                                      │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │                    MCP Server (FastMCP)                        │  │
│  │      stdio / SSE transports • HTTP routes (§5.7, §8.7)         │  │
│  └───────────────────────────────────────────────────────────────┘  │
│            │                                  │                      │
│  ┌─────────┴───────────┐          ┌───────────┴──────────────┐      │
│  │     Intake layer    │          │  Agent-facing facade     │      │
│  │  ┌───────────────┐  │          │  ┌────────────────────┐  │      │
│  │  │ CorpusIntake  │  │          │  │  CognitiveMemory   │  │      │
│  │  │  write        │  │          │  │  retrieve          │  │      │
│  │  │  delete       │  │          │  │  cache_lookup      │  │      │
│  │  │  assert_edge  │  │          │  │  conflict_resolve  │  │      │
│  │  └───────┬───────┘  │          │  │  node_stats        │  │      │
│  │  ┌───────┴───────┐  │          │  │  reinforce_*       │  │      │
│  │  │  WatchIntake  │  │          │  │  (scouts, PTS,     │  │      │
│  │  │  upsert       │  │          │  │   working memory,  │  │      │
│  │  │  delete       │  │          │  │   receipts)        │  │      │
│  │  │  rename       │  │          │  └─────────┬──────────┘  │      │
│  │  └───────┬───────┘  │          └────────────┼─────────────┘      │
│  └──────────┼──────────┘                       │                    │
│             ▼                                  │                    │
│  ┌─────────────────────────────────────────────┼─────────────────┐  │
│  │              KnowledgeManager (corpus)      │                 │  │
│  │  Per-view private plan/apply pairs (ADR-0001):                │  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌────────────────────┐    │  │
│  │  │ SearchEngine│  │   Graph     │  │ ProvenanceProjection│   │  │
│  │  │ (Tantivy +  │  │ (NetworkX)  │  │ (frontmatter        │   │  │
│  │  │  ChromaDB,  │  │             │  │  derived_from →     │   │  │
│  │  │  hidden)    │  │             │  │  edges.db,          │   │  │
│  │  │             │  │             │  │  reconcile-owned)   │   │  │
│  │  └─────────────┘  └─────────────┘  └────────────────────┘    │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                              │                                      │
│  ┌───────────────────────────┼───────────────────────────────────┐  │
│  │                    Storage Layer                               │  │
│  │  Notes tier of corpus:                                         │  │
│  │  ┌─────────────┐                                               │  │
│  │  │  Markdown   │                                               │  │
│  │  │   Files     │                                               │  │
│  │  └─────────────┘                                               │  │
│  │  Asserted-edge tier of corpus + projection (one DB, two tiers  │  │
│  │  separated by `provenance_type` predicate, ADR-0006):          │  │
│  │  ┌─────────────┐                                               │  │
│  │  │  edges.db   │                                               │  │
│  │  └─────────────┘                                               │  │
│  │  Coordination + cognitive-memory state:                        │  │
│  │  ┌─────────────┐  ┌─────────────┐                              │  │
│  │  │coordination │  │  stats.db   │                              │  │
│  │  │    .db      │  │ (LCMA)      │                              │  │
│  │  └─────────────┘  └─────────────┘                              │  │
│  │  Rebuildable derived caches:                                   │  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │  │
│  │  │  Tantivy    │  │  ChromaDB   │  │  NetworkX   │             │  │
│  │  │  (.tantivy) │  │  (.chroma)  │  │  (.graph)   │             │  │
│  │  └─────────────┘  └─────────────┘  └─────────────┘             │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  Event bus (in-memory) ─── SSE delivery at GET /events (§8.7)        │
└──────────────────────────────────────────────────────────────────────┘
```

### 2.2 Data Flow

1. **Agent write/delete**: MCP tool → `CorpusIntake.write` / `CorpusIntake.delete` →
   ensure agent registered → `KnowledgeManager` mutates the notes tier atomically →
   intake syncs Search, then Graph → emit `note.created` / `note.updated` /
   `note.deleted` event. `ProvenanceProjection` remains reconcile-owned today.
2. **Agent edge assertion**: `lithos_edge_upsert` → `CorpusIntake.assert_edge` →
   `EdgeStore` writes an asserted row in `edges.db` carrying a non-`frontmatter`
   `provenance_type` → emit `edge.upserted` event.
3. **Conflict resolution**: `lithos_conflict_resolve` →
   `CognitiveMemory.conflict_resolve` → update an existing `contradicts` edge in
   `edges.db` directly, optionally add a `supersedes` note update via
   `KnowledgeManager.update`, then emit `edge.upserted`.
4. **Filesystem change**: watchdog observer → `WatchIntake.upsert_from_disk` /
   `delete_from_disk` / `rename_on_disk` → `KnowledgeManager` mutates the notes tier →
   watch intake syncs Search, then Graph → emit `note.created` / `note.updated` /
   `note.deleted` / `note.renamed`. `ProvenanceProjection` is still updated by
   reconcile, not incrementally.
5. **Agent read/retrieve**: `lithos_search` → `SearchEngine` → results.
   `lithos_retrieve`, `lithos_cache_lookup`, `lithos_conflict_resolve`, and
   `lithos_node_stats` route through `CognitiveMemory`, which reads through the
   public surfaces of the three derived views and updates its own state in
   `stats.db`.
6. **Reconcile**: operator → `lithos reconcile` (CLI) → `KnowledgeManager`
   invokes each view's private plan/apply pair. Markdown is never modified.
   In `edges.db`, only projection rows (`provenance_type='frontmatter'`) are
   touched; asserted rows are scoped out by predicate.
7. **Startup**: ensure directories and `coordination.db` → construct
   `SearchEngine` eagerly (embedding model loaded before `create()` returns) →
   check rebuild conditions → load graph cache or rebuild projections → start
   `CognitiveMemory` → ready. The watchdog observer is started by
   `lithos serve --watch`, not by `initialize()`.

**Module reference.** The decomposition above is captured in seven accepted
ADRs under [`docs/adr/`](adr/): 0001 (reconcile lives on `KnowledgeManager`),
0002 (`SearchEngine` hides Tantivy/Chroma), 0003 (`CorpusIntake`),
0004 (`ProvenanceProjection`), 0005 (`CognitiveMemory`), 0006 (broaden Corpus
to include asserted edges in `edges.db`), 0007 (`WatchIntake`), 0008 (entity
extraction: NER + extractor provenance).

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

- **`coordination.db`** — agents, tasks, claims, findings, and the read-access
  audit log (pre-LCMA tables, unchanged; the audit log feeds `GET /audit` and
  `lithos audit`).
- **`edges.db`** — typed/weighted edges. Single `edges` table with columns
  `edge_id`, `from_id`, `to_id`, `type`, `weight`, `namespace`, `created_at`,
  `updated_at`, `provenance_actor`, `provenance_type`, `evidence`,
  `conflict_state`. Created lazily on the first edge write. The table holds
  **two tiers separated by the `provenance_type` predicate** (ADR-0004,
  ADR-0006):
  - Rows with `provenance_type='frontmatter'` are the **provenance projection**
    — a derived view of the notes-tier corpus owned by `ProvenanceProjection`.
    Today reconcile rebuilds these from frontmatter `derived_from_ids`; they
    are not authoritative.
  - Rows with any other `provenance_type` (`agent`, `human`, `rule`, …) are the
    **asserted-edge tier of the corpus**. New asserted edges are written by
    `CorpusIntake.assert_edge` via `lithos_edge_upsert`; `lithos_conflict_resolve`
    updates existing asserted contradiction edges directly through
    `CognitiveMemory` today. Reconcile never touches asserted rows. They are
    agent-authored persistent state and must be backed up alongside `knowledge/`.

  Distinct from the `.graph/` NetworkX wiki-link cache — `edges.db` carries
  semantic and learned relationships; NetworkX continues to power the `links`
  section of `lithos_related`.
- **`stats.db`** — `CognitiveMemory` state. Tables: `node_stats`,
  `coactivation`, `enrich_queue`, `working_memory`, `receipts`, plus the MVP 2
  consolidation-log tables. Created lazily on the first `lithos_retrieve` call
  (when the first receipt is written). All accumulated agent state — no drift
  condition relative to the corpus.

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
confidence: <float 0-1>           # Optional: Confidence score (default: 1.0).
                                  # Normalized on read: non-numeric (null,
                                  # strings, bool) and non-finite (.nan/.inf)
                                  # values fall back to 1.0; out-of-range
                                  # finite numbers clamp to [0.0, 1.0].
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
entities:                         # Optional: extracted entity strings
  - <entity-1>                    # Written by the lithos-enrich worker using spaCy NER
  - <entity-2>                    # plus high-precision heuristics (wiki-links, backtick
                                  # terms, mid-sentence-corroborated proper nouns).
                                  # Markdown structure (headings, tables, bold labels) and
                                  # inline code/filenames/punctuation are never entity
                                  # sources; reference sections are excluded and a per-doc
                                  # cap bounds citation-heavy notes (#313, #320).
entities_extractor: <int>         # Optional: version of the extractor that wrote
                                  # `entities`. Absent => entities are agent-curated and
                                  # the enrichment worker never overwrites them; present
                                  # and stale => the worker re-extracts on its next pass.
                                  # Internal provenance — not settable via MCP tools.
# --- Free-form metadata (#305) ---
# Any frontmatter key NOT listed above is preserved as free-form metadata,
# settable via lithos_write(metadata={...}) and returned by lithos_read /
# lithos_list. Keys must not collide with the reserved fields above. Values may
# be arbitrary JSON-compatible values. Current metadata_match filtering accepts
# only scalar query values and matches either exact scalar equality or
# element-wise membership in stored arrays.
github_repos:                     # Example free-form key (arbitrary name)
  - <owner/repo-1>
  - <owner/repo-2>
github_watch_enabled: <bool>      # Example scalar free-form value
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

Parameters are flat at the MCP boundary but grouped below by role to aid discoverability. The grouping is documentation only and mirrors the canonical field taxonomy in `unified-write-contract.md`.

*Core (required):*

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `title` | string | Yes | Title of the knowledge item |
| `content` | string | Yes | Markdown content (without frontmatter) |
| `agent` | string | Yes | Your agent identifier |

*Identity & metadata:*

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `id` | string | No | UUID to update existing; omit to create new |
| `tags` | string[] | No | List of tags |
| `metadata` | object | No | Free-form key/value metadata persisted to frontmatter (#305). Values may be arbitrary JSON-compatible values. On update: omit/`null` preserves; `{}` clears; a non-empty dict is an additive per-key merge (a key whose value is `null` deletes it). Keys must be strings and must not collide with reserved frontmatter fields, else `status="invalid_input"`. Returned by `lithos_read` (as `metadata.extra`) and `lithos_list` (as each item's `metadata`), and filterable via `lithos_list(metadata_match=...)`. |
| `confidence` | float | No | Confidence score 0-1 (default: 1.0). Integers are accepted and coerced to float; anything else that is not a finite number in [0.0, 1.0] — non-numeric values, bool, NaN/inf, or out-of-range numbers — returns `status="invalid_input"`. |
| `path` | string | No | Either a subdirectory (e.g. `"procedures"`) under which the filename is derived from `title` (slugified) and `.md` appended, OR a full relative file path ending in `.md` (e.g. `"procedures/my-doc.md"`) used verbatim as the filename. Intermediate path segments may not end in `.md` — such inputs return `status="invalid_input"`. Paths that resolve to an already-owned file return `status="path_collision"`. |

*Provenance:*

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `source_url` | string | No | Canonical URL provenance (http/https), dedup key after normalization. Pass `""` to clear on update. |
| `derived_from_ids` | string[] | No | Canonical declared lineage (UUIDs). On create: `null`/omit stores `[]`. On update: `null`/omit preserves existing, `[]` clears, non-empty replaces. Self-references rejected. |
| `source_task` | string | No | Task ID or provenance note (stored as `source` in frontmatter) |

*Freshness:*

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `ttl_hours` | float | No | Relative freshness window; converted to `expires_at` |
| `expires_at` | string | No | Absolute ISO datetime freshness deadline |

*Concurrency:*

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `expected_version` | int | No | Optimistic-locking guard for updates; ignored on create |

*LCMA:*

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `schema_version` | int | No | LCMA schema version (default: 1 on create). Preserved on update if omitted. |
| `namespace` | string | No | LCMA namespace. Persisted only when explicitly passed; derived from path at read time otherwise. |
| `access_scope` | enum | No | `shared` \| `task` \| `agent_private` (default: `shared` on create). `task` requires `source_task`. |
| `note_type` | enum | No | `observation` \| `agent_finding` \| `summary` \| `concept` \| `task_record` \| `hypothesis` (default: `observation` on create). |
| `status` | enum | No | `active` \| `archived` \| `quarantined` (default: `active` on create). |
| `summaries` | object | No | Nested `{short, long}` object. Both keys optional, both must be strings if present. |

**Returns (status envelope):**

The error code is the canonical top-level `status` value (e.g. `status="slug_collision"`); there is no separate `code` discriminator field on `lithos_write` envelopes. `status="error"` is retained as a generic fallback for unforeseen failures.

`{ status: "created", id: string, path: string, version: int, warnings: string[] }`

`{ status: "updated", id: string, path: string, version: int, warnings: string[] }`

`{ status: "duplicate", duplicate_of: { id, title, source_url }, message: string, warnings: string[] }`  *(source-URL dedup only — never used for filesystem-path conflicts; see `path_collision` below)*

`{ status: "invalid_input", message: string, warnings: string[] }`

`{ status: "content_too_large", message: string, warnings: string[] }`

`{ status: "slug_collision", message: string, existing_id: string, warnings: string[] }`

`{ status: "path_collision", message: string, existing_id: string, warnings: string[] }`  *(another doc already owns the requested file path — e.g. caller passed an explicit `path` ending in `.md` that's already in use)*

`{ status: "version_conflict", message: string, current_version: int, warnings: string[] }`

`{ status: "error", message: string, warnings: string[] }`  *(generic fallback)*

**Behavior on update:** If `id` is provided and exists, the agent is added to `contributors` if not already present.

**Update semantics:** Omitted optional fields preserve existing values. Some fields support explicit clear. At the MCP boundary, FastMCP cannot distinguish omitted from `null`, so clearable string fields use `""` (empty string) as the clear signal (e.g., `source_url: ""`). See `unified-write-contract.md` for the full MCP boundary convention.

#### `lithos_note_update`
Patch a note's frontmatter (tags / metadata / title / status) **without resending its body** — the note counterpart to `lithos_task_update`. Use this instead of `lithos_write` whenever only frontmatter changes: it removes the read → reconstruct-body → write round-trip (and the lost-update risk of reproducing the body), because the body is never read into the request. Internally it reuses the same update pipeline as `lithos_write` (the `_write_lock` atomicity and `expected_version` check, the search/graph view sync, and the `note.updated` event), passing the body through untouched.

**Arguments:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `id` | string | Yes | UUID of the note to patch. |
| `agent` | string | Yes | Your agent identifier. |
| `title` | string | No | New title. Omit/`null` preserves. Renaming may change the slug; a collision with another note's slug returns `status="slug_collision"`. |
| `tags` | string[] | No | Omit/`null` preserves; `[]` clears all tags; a non-empty list replaces them. |
| `status` | enum | No | `active` \| `archived` \| `quarantined`. Omit/`null` preserves. An out-of-enum value returns `status="invalid_input"`. |
| `metadata` | object | No | Additive per-key merge into existing frontmatter metadata: a key whose value is `null` deletes it, other keys are set, keys not mentioned are preserved. There is no wholesale-clear affordance — `metadata={}` makes no metadata change (mirroring `lithos_task_update`). Keys must be strings and must not collide with reserved frontmatter fields, else `status="invalid_input"`. |
| `expected_version` | int | No | Optimistic-locking guard; reject with `version_conflict` when the note's current version differs. Omit to skip. |

At least one mutable field (`title`, `tags`, `status`, or a non-empty `metadata`) must be provided; otherwise — including `metadata={}` with no other field — the call returns `status="invalid_input"` and writes no revision.

**Returns (status envelope):**

`{ status: "updated", id: string, path: string, version: int, warnings: string[] }`

`{ status: "invalid_input", message: string, warnings: string[] }`

`{ status: "slug_collision", message: string, existing_id: string, warnings: string[] }`

`{ status: "duplicate", duplicate_of: { id, title, source_url }, message: string, warnings: string[] }`

`{ status: "version_conflict", message: string, current_version: int, warnings: string[] }`

`{ status: "error", code: "note_not_found", message: string }`  *(unknown `id`)*

#### `lithos_read`
Read a knowledge file by ID or path.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `id` | string | No* | UUID of knowledge item |
| `path` | string | No* | File path relative to knowledge/ |
| `max_length` | int | No | Truncate content to N characters (default: unlimited) |
| `agent_id` | string | No | Caller identity recorded in the read-access audit log; defaults to `"unknown"` |

*One of `id` or `path` required.

**Returns:** `{ id, title, content, metadata, links, truncated: boolean, retrieval_count: int }`

**Metadata includes:** the reserved frontmatter fields, `derived_from_ids` (list of source UUIDs, may be empty), and `extra` (the isolated free-form metadata dict written through `lithos_write(metadata=...)`).

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
| `mode` | string | No | `"hybrid"` \| `"fulltext"` \| `"semantic"` \| `"graph"` (default: `"hybrid"`) |
| `tags` | string[] | No | Filter by tags (AND) |
| `author` | string | No | Filter by author |
| `path_prefix` | string | No | Filter by path prefix |
| `threshold` | float | No | Minimum similarity 0-1 for semantic/hybrid/graph (default: 0.5) |
| `seed_ids` | string[] | No | Starting document IDs for `graph` mode. When omitted, seeds are discovered via a fast hybrid pass on `query`. |
| `graph_depth` | int | No | BFS hop depth for `graph` mode, 1–3 (default: 2) |
| `entities` | string[] | No | Filter results to documents whose `entities` frontmatter contains every named entity (exact match, AND). Applies to all modes; resolved via an inverted index and applied as a post-filter (#316). |
| `agent_id` | string | No | Caller identity recorded in the read-access audit log |

**Returns:** `{ results: [{ id, title, snippet, score, path, source_url, updated_at, is_stale, derived_from_ids }] }`

**Modes:**
- `hybrid`: Reciprocal Rank Fusion over full-text and semantic results
- `fulltext`: Tantivy BM25 search
- `semantic`: ChromaDB vector similarity search
- `graph`: Wiki-link graph traversal from `seed_ids` (or auto-discovered seeds), bounded by `graph_depth`

**Notes:**
- Search operates on chunks internally but returns deduplicated documents.
- Entity names are indexed as a Tantivy field and included in the default query fields, so query terms matching a document's entities boost its full-text ranking (and curated entities are findable even when absent from the body).
- In `semantic` mode, the returned `score` is the semantic similarity value.
- Invalid `mode` returns `{ status: "error", code: "invalid_mode", message }`.
- Every returned document is recorded in the read-access audit log, batched per call. `agent_id` defaults to `"unknown"` when omitted.

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
| `metadata_match` | object | No | Filter by free-form metadata (see Filter semantics) |
| `entities` | string[] | No | Filter by entity names from the document's `entities` frontmatter (exact match, AND across the list). Resolved via an in-memory inverted index — no full scan (#316). |

**Returns:** `{ items: [{ id, title, path, updated, tags, source_url, derived_from_ids, metadata }], total: int }`
(`metadata` is the document's free-form key/value dict.)

**Behavior:**
- When `title_contains` is present, Lithos materializes the full base-filtered set, applies the title substring filter in memory, then paginates. This keeps `items` and `total` correct across pages.
- When `content_query` is present, Lithos runs Tantivy first (with `tags`, `author`, and `path_prefix` pushed into the search query), then applies the remaining filters (`since`, `title_contains`, `metadata_match`) before pagination.
- `content_query` backend failures return `{ status: "error", code: "search_backend_error", message }`.
- `metadata_match` is resolved through an in-memory inverted index, so a metadata-filtered list never scans the whole knowledge base.
- `entities` is resolved through the same inverted-index machinery; with `content_query` it is applied as a candidate-set intersection after ranking.

**Filter semantics (`metadata_match`, #306):**
- AND across keys: a document must match every `key: q` pair.
- Per key, a document matches when its stored value **equals `q`** or **is a list containing `q`** (e.g. a note with `github_repos: ["org/a","org/b"]` matches `{"github_repos": "org/a"}`).
- Query values must be JSON scalars (string/number/boolean); `null`/list/dict query values return `{ status: "invalid_input", message, warnings: [] }`. Matching is type-sensitive (`"1"` ≠ `1`).

### 5.2 Graph Operations

#### `lithos_tags`
List all tags with document counts.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `prefix` | string | No | Case-insensitive prefix filter on tag names |

**Returns:** `{ tags: { name: count, ... } }`

**Note:** To find documents with a specific tag, use `lithos_list(tags=["tag-name"])`.

#### `lithos_related`

Composite "what is this document related to?" view. Merges wiki-link navigation, derived-from provenance, and typed LCMA edges into a single response.

This tool replaces the previously separate `lithos_links` and `lithos_provenance` tools. Both were pure subsets of `lithos_related` and were removed pre-1.0 to keep the MCP tool count tight. For edge-table queries that are not centred on a single document (e.g. "list all `contradicts` edges"), use `lithos_edge_list` — that tool is the only way to express filters like `type` alone or `to_id` alone.

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
- `lithos_edge_list` remains available for edge-table queries not centred on a single document (global filter by type, namespace, etc.).
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
| `metadata` | object | No | Arbitrary JSON metadata dict persisted on the task row at insert time. Omitted (or `null`) creates a task with empty metadata. This is an **initial set**, not a merge — the row does not yet exist, so there is nothing to merge into. Subsequent mutations go through `lithos_task_update`, which applies an additive per-key merge (see that tool's **Behavior** for the contract). Must **not** contain `depends_on`/`blocked_on` — dependencies are first-class task edges now (use `depends_on` below or `lithos_task_edge_upsert`); a write containing those keys is rejected with `invalid_metadata_key`. |
| `task_type` | string | No | First-class task type: `task`, `epic`, or `gate`; any other value is rejected with `invalid_task_type`. Defaults to `task`. An `epic` (roll-up container) and a `gate` (external wait) are both excluded from `lithos_task_ready`. A `gate` requires gate metadata (see Gates below) — invalid gate metadata is rejected with `invalid_input`. |
| `depends_on` | string[] | No | Predecessor task IDs. Each creates a `blocks` edge `predecessor -> this task`, so this task is not ready until every predecessor is `completed`. Predecessors must already exist (else `task_not_found`). A brand-new task has no outgoing edges, so `depends_on` can never form a cycle. |
| `parent_task_id` | string | No | Optional parent. Creates a `parent_child` edge `parent -> this task` (purely structural; never blocks the child). The parent must exist (else `task_not_found`) and may be any task type. |

**Returns:** `{ task_id: string }` on success, or `{ status: "error", code, message }` on validation failure (codes: `invalid_metadata_key`, `invalid_task_type`, `task_not_found`).

#### `lithos_task_update`
Update mutable task fields.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Task ID |
| `agent` | string | Yes | Agent making the update |
| `title` | string | No | Replacement title |
| `description` | string | No | Replacement description |
| `tags` | string[] | No | Replacement tags |
| `metadata` | object | No | Additive per-key merge patch into the existing task metadata dict. See **Behavior** for merge semantics. Must **not** contain `depends_on`/`blocked_on` (rejected with `invalid_metadata_key`); dependencies are task edges. |

**Returns:** `{ success: true, message }` on success, or `{ status: "error", code, message }` on failure (codes: `invalid_input`, `invalid_metadata_key`, `task_not_found`).

**Behavior:**
- At least one of `title`, `description`, `tags`, or `metadata` must be provided.
- A `metadata` patch containing `depends_on`/`blocked_on` (any value, including a `null` delete) is rejected — those keys are no longer read, and closing the write path prevents stale scheduler-invisible dependency state being recreated.
- **Terminal tasks are updatable (#303).** `task_update` works on `completed`/`cancelled` tasks too — useful for annotating an archived task (e.g. a `metadata` snapshot) without reviving it. `task_not_found` now means the task genuinely does not exist. To bring a task back to active work, use `lithos_task_reopen`.
- `metadata` is applied as an **additive per-key merge**: keys with non-null values overwrite the existing value, keys whose value is `null` are deleted from the existing metadata, and keys not mentioned are preserved. `metadata={}` is a no-op (preserves all existing keys); there is no wholesale-clear affordance. To clear a specific key, pass `{"key": null}`. The merge is performed atomically (single `BEGIN IMMEDIATE` transaction) so concurrent writers updating different keys never clobber each other.

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
| `outcome` | string | No | Optional free-text completion summary persisted on the task row and included in the `task.completed` event payload |
| `cited_nodes` | string[] | No | Optional node IDs the caller found useful; used for LCMA reinforcement feedback |
| `misleading_nodes` | string[] | No | Optional node IDs the caller found misleading; used for LCMA reinforcement feedback |
| `receipt_id` | string | No | Optional specific LCMA receipt to bind the feedback to; otherwise the latest receipt for the same `(task_id, agent)` is used |

**Returns:** `{ success: true, unblocked: string[] }` on success, `{ status: "error", code: "task_not_found", message }` if the task does not exist or is not open, or `{ status: "error", code: "receipt_not_found", message }` if feedback references a missing or unrelated receipt. `unblocked` lists task IDs that this completion just made ready (a `blocks` dependent whose last unsatisfied predecessor was this task), so an orchestrator can pick them up without re-polling `lithos_task_ready`.

**Behavior:** Sets task status to `completed`, persists `resolved_at = now`, stores `outcome`, and releases all active claims on the task. When `cited_nodes` / `misleading_nodes` are supplied, Lithos validates them against the bound LCMA receipt before completion, then applies reinforcement side effects after the task closes. If no receipt can be found and no explicit `receipt_id` was supplied, the feedback is silently dropped and the task still completes.

#### `lithos_task_cancel`
Cancel a task and release all claims.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Task ID |
| `agent` | string | Yes | Agent cancelling the task |
| `reason` | string | No | Optional cancellation reason |

**Returns:** `{ success: true }` on success, or `{ status: "error", code: "task_not_found", message }` on failure.

**Behavior:** Marks an open task as `cancelled`, persists `resolved_at = now` (dual-write with `lithos_task_complete` so both terminal transitions populate the same timestamp), and deletes all claims on that task. The optional `reason` is accepted by the MCP surface but is not persisted in SQLite.

#### `lithos_task_reopen`
Move a terminal (`completed`/`cancelled`) task back to `open` — the inverse of complete/cancel.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Task ID to reopen |
| `agent` | string | Yes | Agent performing the reopen |

**Returns:** `{ success: true, reblocked: string[] }` on success, or `{ status: "error", code, message }` on failure (codes: `task_not_found`, `task_not_resolved` — the task is already `open`).

**Behavior:** Sets `status` back to `open`, clears `resolved_at` and `outcome`, posts a durable `[Reopened]` finding recording the prior terminal status, and emits a `task.reopened` event. Claims were already released on complete/cancel, so none are restored. `reblocked` lists the open dependents this reopen put back under the task's block (via `blocks`/`waits_on_gate` edges) — non-empty only when reopening a **completed** blocker/gate (a dependent that was ready is blocked again); reopening a **cancelled** blocker/gate instead *un-strands* its dependents (`blocker_unsatisfiable` → waiting) and re-blocks no one, so `reblocked` is `[]`. This is the remediation for dependents stranded by a cancelled blocker/gate (see Gates / readiness).

#### `lithos_task_list`
List tasks with optional filters.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `agent` | string | No | Filter by creating agent |
| `status` | string | No | Filter by task status: `open`, `completed`, or `cancelled` |
| `task_type` | string | No | Filter by first-class task type (`task`/`epic`/`gate`) |
| `tags` | string[] | No | Filter to tasks containing all listed tags |
| `since` | string | No | Filter by `created_at >= since` (ISO datetime) |
| `resolved_since` | string | No | Filter by `resolved_at >= resolved_since` (ISO datetime). `resolved_at` is set on both terminal transitions (`complete` and `cancel`), so this surfaces tasks resolved in either way within the window. Open tasks and historical cancellations whose `resolved_at` is `NULL` are excluded automatically. |
| `with_claims` | boolean | No | When `true`, each task in the response includes its active (non-expired) claims inline as a `claims` array (same shape as `lithos_task_status`). Defaults to `false`. Use to avoid an N+1 of `lithos_task_status` calls when rendering a list view. |
| `metadata_match` | object | No | Filter by task metadata. Same semantics as `lithos_list.metadata_match` (AND across keys; stored value equals the query or is a list containing it; scalar query values; type-sensitive). Pushed into SQLite via `json_extract`/`json_each`, so it is engine-evaluated rather than a Python post-scan. Invalid query values return `{ status: "invalid_input", message, warnings: [] }`. |

**Returns:** `{ tasks: [{ id, title, description, status, task_type, created_by, created_at, resolved_at, tags, metadata, outcome }] }`. `resolved_at` is `null` for open tasks (and for historical cancellations from before the dual-write was added). `outcome` is `null` until the task is completed with an outcome. When `with_claims=true`, each task also carries `claims: [{ agent, aspect, expires_at }]`.

#### `lithos_task_status`
Get the full record of a task along with its active claims.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Specific task ID |

**Returns:** `{ tasks: [{ id, title, description, status, task_type, created_by, created_at, resolved_at, tags, metadata, outcome, claims: [{ agent, aspect, expires_at }] }] }`. Returns `{ tasks: [] }` when the task does not exist (mirrors historical behaviour — does not return the error envelope). Use `lithos_task_get` when you want a single-task fetch with an explicit not-found envelope and don't need claims.

**Claim expiry handling:** Expired claims (where `expires_at < now()`) are automatically excluded from results. Cleanup is lazy—expired claims are filtered at query time rather than eagerly deleted.

#### `lithos_task_get`
Fetch a single task by ID. Returns the full task record without claims; use `lithos_task_status` when claims are needed.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Task ID |

**Returns (success):** `{ task: { id, title, description, status, task_type, created_by, created_at, resolved_at, tags, metadata, outcome } }`

**Returns (unknown task):** `{ status: "error", code: "task_not_found", message: string }`

#### Task Graph (Phase 1)

The task graph makes dependencies first-class via a `task_edges` table (see §7) and a `task_type` column. Lithos remains **passive**: readiness is computed at query time from edges and task status; nothing polls external systems. The accepted edge types grow per delivery phase — Phase 1 ships `blocks` (blocking), `parent_child` (structural, non-blocking), and `discovered_from` (provenance, non-blocking); Phase 3 adds `waits_on_gate` (blocking, gate-resolved); deferred relational types are still rejected.

**Readiness predicate.** A task is *ready* when it is `open`, is not a `gate`/`epic`, has no incoming blocking edge whose predecessor is unsatisfied (a `blocks` predecessor must be `completed` — a predecessor still `open`, or terminal-but-cancelled, leaves the edge unsatisfied), and is not held by an unresolved gate. A predecessor that ends `cancelled` leaves its dependents **permanently** blocked (`blocker_unsatisfiable`) rather than spuriously ready — Lithos refuses to call them ready and explains why, leaving re-open/re-route/cancel to the orchestrator.

#### `lithos_task_edge_upsert`
Create or update a typed relation between two tasks.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `from_task_id` | string | Yes | Source task (blocker / parent / source) |
| `to_task_id` | string | Yes | Target task (blocked / child / discovered) |
| `type` | string | Yes | Edge type accepted this phase: `blocks`, `parent_child`, `discovered_from` |
| `agent` | string | Yes | Agent creating the edge |
| `metadata` | object | No | Optional edge metadata (replaced on conflict) |

**Returns:** `{ success: true }`, or `{ status: "error", code, message }` (codes: `invalid_edge_type`, `self_edge`, `task_not_found`, `cycle`, `not_a_gate`). Cycles in blocking edges are rejected on write via a bounded traversal over the `task_edges` indexes (never a full-table walk). A `waits_on_gate` edge requires its `from_task` to be a `gate` task (else `not_a_gate`).

#### `lithos_task_edge_list`
List edges touching a task.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Task whose edges to list |
| `direction` | string | No | `incoming`, `outgoing`, or `both` (default) |
| `types` | string[] | No | Optional edge-type filter |

**Returns:** `{ edges: [{ from_task_id, to_task_id, type, direction, metadata, created_by, created_at }] }`. `direction` is relative to `task_id`.

#### `lithos_task_ready`
Return open tasks whose blocking predecessors are all satisfied (the feasible frontier).

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `project` | string | No | Shorthand for `metadata.project == project` |
| `tags` | string[] | No | Filter to tasks containing all listed tags |
| `metadata_match` | object | No | Metadata filter (same semantics as `lithos_task_list.metadata_match`) |
| `limit` | int | No | Maximum tasks to return (default 50) |
| `with_claims` | boolean | No | Attach each task's active claims inline (default `true`) |

**Returns:** `{ tasks: [...] }`. Claims are **attached** when `with_claims` but never used to exclude a task — collision-correctness comes from the atomic claim, and claims are per-aspect, so the picking agent decides what "taken" means. The query first restricts to the indexed `status='open'` frontier, then applies an index-driven anti-join over blocking edges, so cost scales with the open frontier rather than total task count.

#### `lithos_task_blocked`
Return open tasks that are not ready, each with structured blocker reasons.

**Arguments:** same filter surface as `lithos_task_ready` (no `with_claims`).

**Returns:** `{ tasks: [{ ..., blockers: [{ kind, task_id, type, status, message }] }] }`. `kind` is one of `task` (predecessor still `open` — just waiting), `gate` (waiting on an unresolved gate — see Gates below), `blocker_unsatisfiable` (predecessor or gate `cancelled` — needs intervention), or `cycle` (the blocking chain forms a dependency cycle; `message` names the members).

#### Hierarchy & Spawn (Phase 2)

Hierarchy uses the `parent_child` edge (purely structural — never blocks the child); `epic` is a roll-up container task type (creatable from Phase 2, excluded from `ready`). Hierarchy is a **forest**: a task has at most one parent — a `parent_child` edge to a child that already has a different parent is rejected with `parent_exists` (re-parenting requires removing the existing edge first). It is also kept acyclic: an edge that would make a task its own ancestor is rejected with `cycle` (the same bounded-traversal check `blocks` edges use). There are **no epic close rules yet** (an epic may still complete with open children — deferred to Phase 4).

#### `lithos_task_children`
Return the child tasks of a parent/epic.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Parent whose children to list |
| `recursive` | boolean | No | Walk the full descendant subtree (default `false`) |
| `include_closed` | boolean | No | Include completed/cancelled children (default `false` = open only). The subtree is traversed in full regardless, so an open grandchild under a closed child is still surfaced. |

**Returns:** `{ tasks: [...] }` — task records (same shape as `lithos_task_list`), child tasks via outgoing `parent_child` edges, ordered by `created_at` within each parent.

#### `lithos_task_spawn`
Create a follow-on task linked to an existing source task.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `source_task_id` | string | Yes | The task this follow-on came from |
| `title` | string | Yes | Title for the spawned task |
| `agent` | string | Yes | Spawning agent identifier |
| `description` | string | No | Description for the spawned task |
| `relation_type` | string | No | `discovered_from` (default; non-blocking provenance) or `blocks` (spawned waits until source is `completed`). The edge is always `source -> spawned`. |
| `inherit_project` | boolean | No | Copy `metadata.project` from the source (default `true`) |
| `inherit_tags` | boolean | No | Copy the source's tags (default `true`) |
| `inherit_context` | boolean | No | Copy scheduling-convention metadata (`priority`, `parallelizable`, `phase`) from the source (default `true`). Forbidden keys are never inherited. |
| `metadata` | object | No | Extra metadata; overrides inherited keys. Must not contain `depends_on`/`blocked_on`. |

**Returns:** `{ task_id: string }`, or `{ status: "error", code, message }` (codes: `invalid_relation_type`, `task_not_found`, `invalid_metadata_key`). The spawned task is always `task_type='task'`.

#### Gates (Phase 3)

A **gate** is an external wait modelled as an ordinary task with `task_type='gate'`. It is created via `lithos_task_create` (no dedicated tool) and a task waits on it via a `waits_on_gate` edge (`gate -> task`). Gates are excluded from `lithos_task_ready`.

**Gate metadata** (validated at creation; invalid → `invalid_input`):

| Key | Required | Description |
|-----|----------|-------------|
| `gate_type` | Yes | One of `human`, `timer`, `ci`, `pr`, `external_task` |
| `ready_at` | `timer` only | ISO datetime; the gate auto-resolves once `ready_at <= now`. Normalized to a canonical UTC, second-precision ISO string at creation (a naive value is read as UTC). |

Other type-specific keys (`approval_required_from`, `provider`, `run_id`, `repo`, `pr_number`, `external_id`, `required_state`, …) are advisory — they tell the resolving agent what to check; Lithos does not read them.

**Resolution model — Lithos never polls.** A `waits_on_gate` edge blocks its waiter until the gate is **resolved**:

- the gate task is `completed` (an agent observed the condition and completed it), **or**
- the gate is an `open` `timer` gate whose `ready_at` has passed (evaluated at query time; no state change).

A **cancelled** gate is **unsatisfiable** — its waiter is excluded from `ready` and surfaced in `lithos_task_blocked` with `kind="blocker_unsatisfiable"` (the awaited condition will not be met). "Proceed anyway" is expressed by *completing* the gate or removing the edge, not by cancelling it. An open, not-yet-resolved gate surfaces as a `kind="gate"` blocker. This mirrors `blocks` (`completed` = satisfied, `cancelled` = unsatisfiable) plus the `timer` auto-resolve. Completing a gate reports its newly-ready waiters in the completion's `unblocked` list.

Two invariants keep this sound: a `waits_on_gate` blocker must be a `gate` task (enforced on edge write — `not_a_gate`), and a gate's metadata is re-validated on `lithos_task_update` so it can't be mutated invalid. The readiness predicate is additionally NULL-safe, so an unknown/missing gate state defaults to *blocked*, never spuriously ready.

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

These tools are additive to the pre-LCMA surface — they do not replace `lithos_search`, `lithos_read`, or `lithos_related`. See `docs/plans/lcma-design.md` for the design rationale.

All LCMA tools delegate to the `CognitiveMemory` module (ADR-0005); the MCP
layer is a thin envelope that wraps the module's public methods.
`lithos_edge_upsert` routes through `CorpusIntake.assert_edge` and lands in
`edges.db` as an asserted-tier row (see §3.1). `lithos_conflict_resolve`
currently updates an existing `contradicts` edge directly, then emits the same
`edge.upserted` event shape.

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
  "receipt_id": "rcpt_<short-uuid>",
  "degraded": false,
  "failed_scouts": []
}
```

The `id`/`title`/`snippet`/`score`/`path`/`source_url`/`updated_at`/`is_stale`/`derived_from_ids` keys mirror the `lithos_search` shape so clients that read only those fields work unchanged. `reasons`/`scouts`/`salience` are LCMA-only additive fields. `degraded`/`failed_scouts` are always present: `failed_scouts` lists the canonical names of any scouts whose backend raised (so one bad backend degrades rather than kills the retrieve), and `degraded` is `true` when that list is non-empty — letting a caller distinguish partial results from a genuinely empty corpus.

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
| `evidence` | object \| array \| null | No | Anchors/snippets supporting the edge. Scalars are rejected as `invalid_input`. |
| `conflict_state` | string | No | Reserved for `contradicts` edges (MVP 2). |

**Returns:** `{ "status": "ok", "edge_id": "edge_<short-uuid>" }`.

**Side effect:** emits an `edge.upserted` event on the in-memory event bus (see §8.2).

#### `lithos_edge_list`

Query edges by node, type, or namespace.

**Arguments:** `from_id`, `to_id`, `type`, `namespace` — all optional. Filters compose as `AND`.

**Returns:** `{ "results": [ { edge_id, from_id, to_id, type, weight, namespace, created_at, updated_at, provenance_actor, provenance_type, evidence, conflict_state } ] }`.

#### `lithos_conflict_resolve`

Resolve a contradiction between two notes by setting the `conflict_state` on a
`contradicts` edge. The resolution is recorded so future retrieval reflects it,
and an `edge.upserted` event is emitted carrying the new `conflict_state`.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `edge_id` | string | Yes | Edge ID of the `contradicts` edge to resolve |
| `resolution` | string | Yes | One of `accepted_dual` \| `superseded` \| `refuted` \| `merged` |
| `resolver` | string | Yes | Agent or user identifier performing the resolution |
| `winner_id` | string | No | Required when `resolution="superseded"`; must equal the edge's `from_id` or `to_id`. The winner is also marked as superseding the loser via `lithos_write` (`supersedes` field). |

**Returns:** `{ "status": "ok", "edge_id": "...", "conflict_state": "..." }` on
success, or `{ "status": "error", "code": "...", "message": "..." }` on failure.
Error codes:

- `invalid_input` — `resolution` not in the allowed set; edge is not a
  `contradicts` edge; `winner_id` missing when `resolution="superseded"`;
  `winner_id` is not one of the edge's endpoints.
- `not_found` — no edge with the given `edge_id` exists.
- `update_failed` — the edge lookup succeeded but the persistence write did
  not (e.g. concurrent deletion).

**Side effect:** emits `edge.upserted` (see §8.2) carrying the new
`conflict_state`.

#### `lithos_node_stats`

Inspect a single node's `CognitiveMemory` state — salience, retrieval counts,
and reinforcement penalty fields — by reading from `stats.db.node_stats`.

**Arguments:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `node_id` | string | Yes | Document ID to look up |

**Returns (success):** `{ node_id, salience, retrieval_count, cited_count, last_retrieved_at, last_used_at, ignored_count, misleading_count, decay_rate, spaced_rep_strength, last_decay_applied_at }` — verbatim row from `stats.db.node_stats` for the given node. Counts and timestamps default to `0` / `null` until the node accrues retrieval activity.

**Returns (not found):** `{ status: "error", code: "doc_not_found", message }` when `node_id` does not match any document.

**Use case:** lets an agent or operator inspect why retrieval is or isn't
surfacing a particular document, without having to query SQLite directly.

### 5.7 HTTP Endpoints

These endpoints are standard HTTP routes mounted alongside the MCP transport. They are **not** MCP tools and do not appear in `tools/list`. The server mounts three: `GET /health`, `GET /events`, and `GET /audit`.

When run with `--transport http`, the server exposes both MCP transport endpoints on the same port: `POST /mcp` (StreamableHTTP, MCP 2025-03-26+, stateless) and `GET /sse` + `POST /messages/` (legacy SSE). Any compliant MCP client can connect to whichever it supports, with no bridge or proxy. The three custom routes above are served on the same port.

#### `GET /health`

Lightweight health check for Docker `HEALTHCHECK`, load balancers, and monitoring.

**Returns (200 OK):**
```json
{
  "status": "ok",
  "timestamp": "2026-03-18T12:00:00+00:00",
  "components": {
    "kb_directory": { "status": "ok" },
    "search": { "status": "ok" },
    "knowledge_base": { "status": "ok" }
  }
}
```

**Returns (503 Service Unavailable):** Same shape with `"status": "degraded"` and per-component `error` strings for any unhealthy component.

**Components checked:**

| Component | Check |
|-----------|-------|
| `kb_directory` | Knowledge base directory exists on disk |
| `search` | `SearchEngine.health()` passes (composed full-text + semantic signal) |
| `knowledge_base` | Can list at least one document |

**Use case:** The Docker image uses this endpoint in its `HEALTHCHECK` directive (`curl -f http://localhost:8765/health`). External orchestrators and load balancers can poll it to determine readiness.

#### `GET /events`

Server-Sent Events delivery surface for the in-memory event bus. The contract,
query parameters (`types`, `tags`, `since`), `Last-Event-ID` header behavior,
ring-buffer replay, keepalive, `429`, `503`, and auth gating are all defined
in §8.7. This row is here so §5.7 enumerates every HTTP route the server
mounts.

#### `GET /audit`

Read-only access to the audit log of document reads (search results returned,
documents fetched, etc.). Useful for offline analysis and debugging retrieval
behavior.

**Query parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `agent_id` | string | No | Filter to entries reported by this agent |
| `after` | string | No | ISO-8601 timestamp; only entries after this time |
| `limit` | int | No | Max entries (default: 100) |
| `doc_id` | string | No | Filter entries for a specific document |

**Returns (200 OK):** `{ "entries": [ { "id", "agent_id", "doc_id", "operation", "timestamp" } ] }`

**Returns (400):** `{ "error": "invalid_after", "message": "..." }` when `after` cannot be parsed.

**Returns (503):** `{ "error": "audit_log_unavailable", "entries": [] }` when the coordination layer fails.

**Trust boundary:** the endpoint is **unauthenticated** in the current
implementation and `agent_id` values are self-reported by callers, so the audit
log is advisory-only and must not be used for access control. Suitable only for
trusted-network deployments. When MCP authentication lands, this endpoint must
be gated behind it.

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
6. Finish eager `SearchEngine.create()` initialization, including embedding-model load

**File watcher startup:** The server supports watching, but the watcher is started by the CLI `serve` command when `--watch` is enabled. `initialize()` does not start it by itself.

**On-demand rebuild** via `lithos reindex --clear`.

### 6.2 File Change Handling

| Event | Action |
|-------|--------|
| File created | Parse, chunk, add to all indices |
| File modified | Parse, re-chunk, update all indices |
| File deleted | Remove from all indices |
| File moved/renamed | `WatchIntake.rename_on_disk` handles in-corpus renames, enter-corpus moves, and leave-corpus moves explicitly |

**Note on renames and wiki-links:** `WatchIntake.rename_on_disk` preserves the
document id and rebinds Search and Graph state to the new path. However,
wiki-link text in *other* files still points to the old path until those files
are updated. `lithos validate` reports these as broken links.

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
  task_type TEXT NOT NULL DEFAULT 'task',  -- task (Phase 1); epic (Phase 2); gate (Phase 3)
  created_by TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  tags JSON,
  outcome TEXT,                -- free-text summary set by lithos_task_complete
  resolved_at TIMESTAMP,       -- dual-written on both terminal transitions (complete and cancel); NULL while open
  metadata JSON
);

-- Task graph edges (ordering, hierarchy, provenance)
CREATE TABLE task_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_task_id TEXT NOT NULL,  -- source (blocker / parent / source)
  to_task_id TEXT NOT NULL,    -- target (blocked / child / discovered)
  type TEXT NOT NULL,          -- blocks | parent_child | discovered_from (Phase 1)
  metadata JSON,
  created_by TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (from_task_id) REFERENCES tasks(id),
  FOREIGN KEY (to_task_id) REFERENCES tasks(id),
  UNIQUE(from_task_id, to_task_id, type)
);
-- Both directions indexed: ready/blocked queries and cycle detection must stay sub-linear
CREATE INDEX idx_task_edges_from ON task_edges(from_task_id, type);
CREATE INDEX idx_task_edges_to ON task_edges(to_task_id, type);

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

**Migration & backfill.** Schema changes are applied as idempotent, column-presence-guarded `ALTER`s at `initialize()` time (no separate migration tool). When `tasks.task_type` is first added to an existing database, a **one-time backfill** runs in the same migration transaction: open tasks' legacy `metadata.depends_on` / `metadata.blocked_on` values become canonical `blocks` edges (marked `{"migrated_from": ...}` in edge metadata), references to nonexistent task IDs are logged and skipped, and any edges that form a cycle are retained (cycle members are excluded from `ready` and surfaced as `cycle` blockers). After the backfill, `task_edges` is the **only** thing the scheduler reads; `metadata.depends_on`/`blocked_on` are no longer read, and writing them via `lithos_task_create`/`lithos_task_update` is rejected so stale scheduler-invisible state cannot be recreated. The backfill is tied to the column-addition branch, so it runs exactly once and is a no-op on fresh databases (which have no legacy dependency metadata).

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
| `note.updated` | `lithos_write` (update), `lithos_note_update` (frontmatter patch), file watcher (create/modify) | `id`, `title`, `path` (tool); `path` (watcher) |
| `note.deleted` | `lithos_delete`, file watcher (delete) | `id`, `path` (tool); `path` (watcher) |
| `note.renamed` | `WatchIntake.rename_on_disk` (in-corpus rename detected by the watcher) | `id`, `src_path`, `dest_path` |
| `edge.upserted` | `lithos_edge_upsert` via `CorpusIntake.assert_edge`; also `lithos_conflict_resolve` when it updates a `contradicts` edge | `edge_id`, `from_id`, `to_id`, `type`, `namespace` (assert_edge); `edge_id`, `from_id`, `to_id`, `type`, `conflict_state` (conflict_resolve) |
| `task.created` | `lithos_task_create` | `task_id`, `title` |
| `task.updated` | `lithos_task_update` | `task_id` |
| `task.claimed` | `lithos_task_claim` | `task_id`, `agent`, `aspect` |
| `task.released` | `lithos_task_release` | `task_id`, `agent`, `aspect` |
| `task.completed` | `lithos_task_complete` | `task_id`, `agent`, `outcome`, `cited_nodes`, `misleading_nodes`, `receipt_id` |
| `task.cancelled` | `lithos_task_cancel` | `task_id`, `agent`, `reason` |
| `task.reopened` | `lithos_task_reopen` | `task_id`, `agent`, `prior_status`, `prior_outcome` |
| `finding.posted` | `lithos_finding_post` | `finding_id`, `task_id`, `agent` |
| `agent.registered` | `lithos_agent_register` | `agent_id`, `name` |

For `task.completed`, the current implementation emits `cited_nodes`, `misleading_nodes`, and `receipt_id` as JSON-encoded strings in the event payload (for example `"[\"node-1\"]"` or `"null"`), while `outcome` is emitted as a normal string or `null`.
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

When the reconnect id is not in the ring — evicted past the buffer horizon, carried over from a previous server run, or otherwise unknown — replay cannot be proven complete. Rather than silently under-deliver, `get_buffered_since` reports a gap and the SSE surface emits a `resync` control event (see §8.7) so the client re-fetches current state instead of trusting a truncated replay.

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
- When the supplied id is not in the ring buffer (evicted, from a previous server run, or unknown), emits an `event: resync` control message (no `id:` line, so it never becomes the client's next `Last-Event-ID`) instructing the client to resync from current state, then continues streaming live events.
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
  transport: stdio          # stdio | http (http serves both /mcp and /sse)
  host: 127.0.0.1          # Default bind address
  port: 8765               # For the HTTP transport
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

# Run with HTTP transport (serves both /mcp StreamableHTTP and /sse)
lithos --data-dir ./data serve --transport http --host 0.0.0.0 --port 8765

# Disable file watcher
lithos --data-dir ./data serve --no-watch

# Route OTEL metrics + spans to stdout (local debugging without a collector)
lithos --data-dir ./data --telemetry-console serve

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

# Show the read-access audit log (HTTP equivalent: GET /audit)
lithos --data-dir ./data audit
lithos --data-dir ./data audit --agent agent-zero --limit 100
lithos --data-dir ./data audit --doc <id> --since 2026-01-01T00:00:00
```

---

## 10. Error Handling

### 10.1 Current Behavior

Tools indicate routine domain failures through return values, and unexpected backend failures may still surface as MCP-level exceptions.

- **Structured status envelopes**: `lithos_write` returns `{ status: "error", code, message, ... }` for invalid input and contract-level failures
- **Structured error envelopes on many tools**: `lithos_delete`, `lithos_task_create`, `lithos_task_claim`, `lithos_task_renew`, `lithos_task_release`, `lithos_task_complete`, `lithos_task_update`, `lithos_task_cancel`, `lithos_task_get`, `lithos_task_edge_upsert`, `lithos_search`, `lithos_list`, and `lithos_cache_lookup` use `{ status: "error", code, message }` for routine domain failures
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
| Updating an unknown task | `lithos_task_update` returns `{ status: "error", code: "task_not_found" }` (terminal tasks are updatable — #303; only genuinely-missing ids error) |
| Cancelling unknown/closed task | `lithos_task_cancel` returns `{ status: "error", code: "task_not_found" }` |
| Reopening unknown task / already-open task | `lithos_task_reopen` returns `{ status: "error", code: "task_not_found" }` / `{ code: "task_not_resolved" }` |
| Fetching unknown task via `lithos_task_get` | Returns `{ status: "error", code: "task_not_found" }` |
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
- ~~Structured `source` provenance with `derived_from` links to source knowledge items~~ (Implemented in Phase 3 via `derived_from_ids`, exposed through `lithos_related`)
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
→ lithos_task_complete(task_id="task-456", agent="agent-zero", outcome="Captured the main asyncio.gather patterns and tradeoffs.")
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
| Knowledge | `lithos_write`, `lithos_note_update`, `lithos_read`, `lithos_delete`, `lithos_search`, `lithos_list`, `lithos_cache_lookup` |
| Graph | `lithos_tags`, `lithos_related` |
| Agent | `lithos_agent_register`, `lithos_agent_info`, `lithos_agent_list` |
| Coordination | `lithos_task_create`, `lithos_task_update`, `lithos_task_claim`, `lithos_task_renew`, `lithos_task_release`, `lithos_task_complete`, `lithos_task_cancel`, `lithos_task_reopen`, `lithos_task_list`, `lithos_task_status`, `lithos_task_get`, `lithos_finding_post`, `lithos_finding_list` |
| Task Graph | `lithos_task_edge_upsert`, `lithos_task_edge_list`, `lithos_task_ready`, `lithos_task_blocked`, `lithos_task_children`, `lithos_task_spawn` |
| System | `lithos_stats` |
| LCMA (Phase 7 MVP 1) | `lithos_retrieve`, `lithos_edge_upsert`, `lithos_edge_list`, `lithos_conflict_resolve`, `lithos_node_stats` |
| HTTP | `GET /health`, `GET /events`, `GET /audit` (not MCP tools; see §5.7 and §8.7) |

**Total: 37 MCP tools + 3 HTTP endpoints** (`lithos_note_update` adds a frontmatter-only note patch — tags/metadata/title/status without the body — at parity with `lithos_task_update` (#362); task graph Phase 1 added `lithos_task_edge_upsert`, `lithos_task_edge_list`, `lithos_task_ready`, and `lithos_task_blocked`; Phase 2 added `lithos_task_children` and `lithos_task_spawn` plus `parent_task_id`/`epic` on create; Phase 3 added the `gate` task type and `waits_on_gate` edge with no new tools — gates are created via `lithos_task_create` and resolved via `lithos_task_complete`; `lithos_task_reopen` completes the lifecycle (terminal → open, the remediation for stranded dependents) and `lithos_task_update` now accepts terminal tasks (#303); `lithos_task_get` is in the coordination surface; LCMA gained `lithos_conflict_resolve` and `lithos_node_stats` to surface contradiction resolution and per-node retrieval stats; the SSE delivery surface at `/events` and the read-access audit log at `/audit` are now first-class HTTP endpoints alongside `/health`)

---

**End of Specification**
