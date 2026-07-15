---
status: accepted
---

# Entity extraction: spaCy NER + corroborated heuristics, with per-field extractor provenance

The enrichment worker's rule-based entity extractor harvested Markdown section headings and sentence-initial capitalized words as entities (#313). Measured on the prod corpus (2026-06-07): 890/1,311 notes carried extracted entities, mean **53.8 entities per note** (max 663), with the document-frequency table dominated by template structure — `Summary` (464 notes), `Notes` (416), `Source` (388), `Key` (365) — at an estimated 9–16% precision. Every entities list in the corpus was extractor-written; no agent-curated entities existed. Two structural defects compounded the noise: the extractor maintained its own 201-word common-words list (#174), and the worker's skip-if-non-empty guard meant extraction quality could never improve on existing notes — junk written once was junk forever. We are replacing the extractor with a three-source union — author-asserted signals (wiki-links, backticks), spaCy `en_core_web_sm` NER, and positionally corroborated capitalization heuristics — gated by a new `entities_extractor` frontmatter version marker that lets the worker re-extract its own stale output while never touching agent-curated entities. Validated against the prod corpus: mean 11.2 entities per note with junk classes eliminated on sampled notes.

## Considered Options

- **Tighten the heuristics only (no NER).** Rejected — a prototype with heading-stripping and mid-sentence corroboration cut the mean from 49.0 to 11.2 entities/note on a 200-note prod sample, but is positionally blind: entities that only ever appear sentence-initial or in headings (`Festo`, `KD Software`) are unrecoverable, and emphasis markers split multi-word names (`Broadridge Financial Solutions` → three fragments). The recall ceiling is structural, not tunable.
- **Wiki-links and backticks only.** Rejected — zero noise by construction, but article notes (the bulk of the corpus) contain no wiki-links; entities would effectively vanish for the note class that motivated the fix.
- **LLM-based extraction via `LcmaConfig.llm_provider`.** Rejected for now — best quality ceiling but adds an inference dependency, nondeterminism (breaking idempotent re-extraction), and significant scope to a privacy-first local server. The extractor seam (`lithos.lcma.entities.extract_entities`) leaves room for this later.
- **spaCy as an optional extra (`lithos[ner]`).** Rejected — extraction quality would silently differ by install, and staging/prod must remember the extra. spaCy + `en_core_web_sm` (~12 MB pinned wheel) is modest next to the existing sentence-transformers/ChromaDB footprint. The heuristic path remains as a runtime fallback when the model genuinely cannot load, with a one-time warning.
- **Keep the never-overwrite contract; clean up with one-shot sweeps forever.** Rejected — without per-field provenance every future extractor improvement needs another manually-run corpus repair. A version marker (`entities_extractor`) makes staleness self-describing: the worker re-extracts its own output when the version bumps, and the periodic full sweep heals the corpus without operator action.
- **Doc-level provenance via `contributors`.** Rejected — `contributors` records which agents ever updated a note, not which field they wrote; a note can list `lithos-enrich` for a salience write while a human curated its entities. Field-level provenance must live with the field.
- **NER + corroborated heuristics union, `entities_extractor` marker, hard spaCy dependency.** Accepted.

## Consequences

- Extraction lives in `src/lithos/lcma/entities.py` behind `extract_entities(text) -> list[str]`; `enrich.py` no longer owns patterns or word lists. `ENTITY_EXTRACTOR_VERSION` (int, starts at 2 — version 1 being the retired heading-harvester) must be bumped on any quality-affecting change.
- Three signal sources, unioned: (1) wiki-link targets and backtick terms — author-asserted, always kept; (2) spaCy NER over a structurally cleaned view (code fences and tables removed, ≤3-word headings dropped, sentence-like headings retained as standalone sentences, emphasis markers stripped) filtered to naming labels (PERSON, ORG, PRODUCT, GPE, LOC, FAC, NORP, EVENT, WORK_OF_ART, LANGUAGE); (3) capitalized phrases/proper nouns corroborated by mid-sentence occurrences (phrases ×1, single words ×2). Candidates matching short heading/bold-label text are rejected; single words subsumed by kept phrases are dropped; output is sorted and deduplicated so unchanged content never churns frontmatter.
- The hand-rolled `_COMMON_WORDS` list is deleted; noise filtering uses spaCy's `STOP_WORDS` plus a small calendar-words set (#174). Multilingual corpora remain unsupported (English model, English stop words); the model name is a module constant and the seam admits per-language models later.
- New frontmatter field `entities_extractor: int`, written only by the extractor paths. `KnowledgeManager.update` clears the marker whenever entities are set without it — an agent writing entities thereby curates them. The marker is deliberately not exposed on the MCP tool surface.
- Worker contract (replacing skip-if-non-empty): empty → extract and stamp; stale marker → re-extract (an empty result still clears junk); no marker on non-empty entities → never touch; current marker → skip. `full_sweep` runs the same logic corpus-wide each cycle, so a version bump heals the corpus within one sweep interval.
- One-shot bootstrap: `lithos extract-entities --force` re-extracts every note regardless of markers — required exactly once because the existing corpus predates the marker and would otherwise read as curated. This command mutates the corpus and is therefore not a Reconcile (see CONTEXT.md); operators run `lithos reconcile` afterwards to refresh derived views.
- spaCy ≥3.8 and the pinned `en_core_web_sm` wheel are hard dependencies (`tool.hatch.metadata.allow-direct-references` enabled for the wheel URL). Extraction is degraded-but-functional without the model: author-asserted signals plus corroborated heuristics.

## Amendment — extractor version 3 (#320)

The first prod/staging sweeps (version 2) surfaced two residual precision failures on technical and academic notes:

- **Inline code harvested as entities.** The backtick rule kept every `` `term` `` verbatim, so code-heavy notes produced junk like `merge_and_normalize()`, `note.created`, `scout_freshness`, `"guides"`, `foo.md`, `references/`, `min(0.1, days × 0.005)`. The assumption that backticks mark proper nouns holds for prose but not technical docs, where they mark code.
- **Bibliography explosions.** spaCy NER harvested every cited author name in long papers — one staging doc reached 3,254 entities.

Version 3 changes (all behind the existing `entities_extractor` marker, so a `--force` re-sweep heals the corpus):

- **Single name-shape gate.** Every candidate from every path (backtick, NER, heuristic) must match `^[A-Za-z0-9]+(?:[ '&.+#-][A-Za-z0-9]*)*$` — alphanumeric runs joined by spaces, hyphens, apostrophes, ampersands, dots, or plus/hash. Dots and `+`/`#` are allowed so capitalized product names survive (`Node.js`, `TensorFlow.js`, `GPT-4.1`, `C++`); the companion **bare-lowercase rule** rejects all-lowercase dotted code (`note.created`, `asyncio.gather`, `foo.md`, `metadata.project`) and bare jargon (`node`, `task`, `guides`). Slashes, quotes, brackets, underscores, equals, and other operators are rejected outright. Backtick terms pass through the same `_clean_candidate` gate rather than being trusted verbatim; wiki-link targets remain lenient (explicit author intent). Subsumption treats dots/hyphens as token separators, so a bare prefix (`Node`) is dropped when the fuller name (`Node.js`) is kept.
- **Reference-section stripping.** The reference/bibliography *section* — from a `## References` / `Bibliography` / `Citations` / `Sources` heading up to the next heading at the same or higher level — is dropped before extraction. A later `## Appendix` or postscript is preserved; only citation author-name soup is removed.
- **Per-document cap.** A configurable backstop (`lcma.entity_max_per_doc`, default 50; `0` disables); when exceeded, the most frequently mentioned candidates win (ties broken alphabetically for determinism), and author-asserted wiki-link targets are always kept. Bounds the worst case for inline-citation docs that lack a references heading.

Measured on the prod corpus: mean 12.8 → 10.3, median 8 → 7 (prose notes barely move), max 122 → 46. The `agent-guide-to-using-lithos-via-mcp` note dropped from 213 entities (mostly code) to 33 real ones.

### Amendment — extractor version 4

The v3 staging sweep surfaced two residual classes the gate still let through:

- **Uppercase filenames** (`README.md`, `AGENTS.md`, `settings.json`) — the bare-lowercase rule only caught lowercase `foo.md`, and the product-token rule actively surfaced capitalized filenames from prose. v4 rejects any candidate whose tail is a known document/config/data/script extension, case-insensitively; `.js`/`.ts` are excluded so `Node.js`/`TensorFlow.js` survive.
- **Junk wiki-link targets** (`[[\phi]]`, `[[IntegerPropertyFilter(...)]]`) — wiki-link targets were kept verbatim as author intent, bypassing every other gate. v4 still keeps them lenient but drops a target carrying code punctuation (`()[]{}=<>`), a leading backslash (LaTeX), or a filename extension. Real links (`[[Knowledge Graph]]`, `[[target-doc|display]]`) are unaffected.

`ENTITY_EXTRACTOR_VERSION` → 4; the `--force` re-sweep heals the corpus via the marker contract.

## Amendment — `extract-entities` no longer needs a follow-up reconcile (task ba8d7f25)

The Consequences above say operators run `lithos reconcile` after
`lithos extract-entities --force` to refresh derived views. That is no longer
true, and the requirement was never by design: the command wrote through
`KnowledgeManager.update`, which bypassed the Search/graph re-index and emitted
no event, so the reconcile existed to repair drift the command itself had
manufactured (the same bypass task 681ac952 removed from the enrich worker).

The command now routes through `CorpusIntake.note_update`, which re-indexes the
derived views inline and emits `NOTE_UPDATED` — stamped `origin=enrich` so a
running worker drops the event rather than re-enqueueing the node. Extraction is
still a corpus mutation and therefore still not a Reconcile (see CONTEXT.md);
what changed is that it no longer leaves drift behind for one.
