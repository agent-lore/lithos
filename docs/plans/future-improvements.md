# Future Improvements (Deferred)

This document tracks known gaps, improvement ideas, and open issues that are not addressed by any active implementation plan. Items here are candidates for future plans or early-phase additions to existing plans.

These are not blockers for the current phase roadmap. Each item notes why it is deferred and what a "ship earlier" path might look like.

---

## 1. Conflict Resolution Workflow (Pre-LCMA)

When two agents write contradictory knowledge about the same topic, there is no mechanism beyond "agents handle conflicts themselves" (`SPECIFICATION.md` section 1.2). LCMA MVP 2 (Phase 7) adds `contradicts` edges in `edges.db` with a full conflict state machine (`unreviewed | accepted_dual | superseded | refuted | merged`) and a `lithos_conflict_resolve` tool.

**Ship-earlier option:** A simpler `conflict_flag: bool` field in `KnowledgeMetadata` frontmatter, settable via `lithos_write`, that `lithos_search`/`lithos_semantic` surface as a warning. This requires no new SQLite store. Defer to LCMA MVP 2 unless user demand warrants the simpler flag earlier.

---

## 2. Knowledge Quality Signals (Pre-LCMA)

There is no way to distinguish "this note is frequently retrieved and useful" from "this note was written and never retrieved." LCMA Phase 7 adds `stats.db` with `retrieval_count`, `salience`, coactivation counts, and spaced-repetition decay.

**Ship-earlier option:** Add a `retrieval_count` integer field to `coordination.db` (one row per `doc_id`), incremented by `lithos_read` and search result returns. No new SQLite file needed; `CoordinationService` already manages that database. The simpler counter should be retired or migrated when LCMA `stats.db` ships.

---

## 3. Namespace/Scope from Day One

`access_scope` and `namespace` are LCMA MVP 1 fields (Phase 7 of the roadmap), but search pollution from mixed agent writes is a problem today. An agent writing scratch notes pollutes the shared search space for all other agents.

**Ship-earlier option:** Ship the `access_scope` field with its default value of `shared` as part of Phase 2 or Phase 3, with basic filtering in `lithos_search` and `lithos_semantic` by `namespace` prefix. This is compatible with the existing `path` parameter convention (agents already use subdirectories like `knowledge/agent/<id>/`). The LCMA scout namespace gating would then layer on top at MVP 1.

**Risk:** Shipping an early `access_scope` filter may create behavioral expectations that diverge from the richer LCMA access model. Decision: revisit after Phase 3 exits.

Note: `access_scope` is a retrieval scoping mechanism, not a security boundary. All agents operate in the same trust domain per `SPECIFICATION.md` section 1.2.

---

## 4. Bulk Import for Existing Knowledge

No plan addresses importing an existing Obsidian vault or markdown corpus into Lithos with frontmatter conformance checks. `lithos reindex` handles re-indexing existing conformant documents but does not validate or repair frontmatter.

**Options:**

- A new `lithos import <path>` CLI command that walks a directory, validates frontmatter fields against `KnowledgeMetadata` schema, optionally repairs missing required fields (assigning new UUIDs, setting defaults), and writes conformant files into the `knowledge/` directory before triggering reindex.
- Extend `lithos validate --fix` to handle foreign vaults (repair missing frontmatter in-place).

Natural fit for Phase 9 (`cli-extension-plan.md`). Not a prerequisite for any active plan.

---

## 5. Small Runtime Fixes

Small standalone defects should be fixed directly when discovered rather than queued behind roadmap phases.

Recent example: the `inspect_doc` timestamp attribute mismatch in `cli.py` was corrected directly instead of being assigned to a later CLI phase.
