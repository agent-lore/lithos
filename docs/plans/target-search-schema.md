# Target Search Schema (Registry)

This document is the canonical target schema for search projections.

It exists to prevent drift across plans and to batch schema-breaking rebuilds where possible.

Normative references:

- `unified-write-contract.md`
- `final-architecture-guardrails.md`

## Tantivy Document Fields

Required fields (target end state):

- `id` (stored)
- `title` (text/stored)
- `content` (text/stored)
- `path` (stored)
- `author` (stored)
- `tags` (stored)
- `created_at` (stored)
- `updated_at` (stored)
- `expires_at` (stored, optional)
- `source_url` (stored, raw for exact matching)

Notes:

- URL lookups should use exact term-query helpers, not naive unquoted query strings.
- `expires_at`/`updated_at` are surfaced for freshness; filtering/ranking logic can use them as needed.

## Chroma Chunk Metadata Fields

Required metadata fields (target end state):

- `doc_id`
- `chunk_index`
- `title`
- `path`
- `author`
- `tags`
- `source_url` (optional)
- `updated_at` (optional)
- `expires_at` (optional)

## Rebuild Strategy

When any target field addition/removal requires backend schema changes:

1. detect mismatch at startup
2. recreate affected index
3. run full rebuild in same boot

Batching rule:

- use one combined schema jump for the Phase 2 (`source_url`) and Phase 4 (`updated_at`, `expires_at`) Tantivy changes, followed by one rebuild window.
- if implementation constraints force an earlier partial rollout, document the reason in `implementation-checklist.md` before coding starts rather than silently taking a second rebuild.
- if Phase 4 ships in a release separate from Phase 2, one additional rebuild is acceptable, but the preferred approach is to include all target fields (even as initially empty/unpopulated) in the Phase 2 schema change so the second rebuild is avoided entirely.
