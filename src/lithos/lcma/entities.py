"""Entity extraction for the enrichment worker (#313, #174).

Combines three signal sources, highest precision first:

1. **Author-asserted**: ``[[wiki-link]]`` targets and `` `backtick` `` terms.
2. **NER**: spaCy ``en_core_web_sm`` over a structurally cleaned view of the
   text (code fences and tables removed, label-like headings dropped,
   sentence-like headings kept as standalone sentences).
3. **Positional heuristics** (also the fallback when the model is
   unavailable): capitalized phrases and proper nouns that are corroborated
   by at least one mid-sentence capitalized occurrence — sentence-initial
   capitalization alone is sentence case, not a proper noun.

Markdown *structure* (headings, table cells, bold label lines) is never an
entity source: the prod corpus audit (2026-06-07) showed template headings
such as ``## Summary`` / ``## Profile Relevance`` polluting every enriched
note.

``ENTITY_EXTRACTOR_VERSION`` is the provenance marker written to
``entities_extractor`` frontmatter alongside extracted entities; bump it on
any quality-affecting change so the enrichment worker re-extracts its own
stale output (entities without the marker are agent-curated and never
touched).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from spacy.language import Language

logger = logging.getLogger(__name__)

# Bump on any quality-affecting change to extraction. Version 1 was the
# heading-harvesting heuristic extractor removed by #313. Version 3 added
# strict name-shape validation (rejecting code/punctuation/filenames),
# reference-section stripping, and a per-doc cap (#320). Version 4 rejects
# uppercase filenames (README.md) and guards wiki-link targets against code
# punctuation / LaTeX.
ENTITY_EXTRACTOR_VERSION = 4

_MODEL_NAME = "en_core_web_sm"

# spaCy entity labels that name things worth tracking; DATE/TIME/quantity
# labels are deliberately excluded.
_NER_LABELS = frozenset(
    {
        "PERSON",
        "ORG",
        "PRODUCT",
        "GPE",
        "LOC",
        "FAC",
        "NORP",
        "EVENT",
        "WORK_OF_ART",
        "LANGUAGE",
    }
)

# --- Markdown structure ---
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
_WIKI_LINK_RE = re.compile(r"\[\[([^\]\[|]+?)(?:\|[^\]]+)?\]\]")
_BACKTICK_RE = re.compile(r"(?<!`)`([^`\n]+?)`(?!`)")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
# ``**Label:** value`` / ``- **Label**: value`` pseudo-heading prefixes
_BOLD_LABEL_RE = re.compile(r"^(\s*(?:[-*+]\s+)?)\*\*([^*\n]{1,60})\*\*[:\s]", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"\*{1,2}|_{2}")
# A reference/bibliography heading and everything after it — citation lists are
# author-name soup, not document entities (#320).
_REFERENCES_HEADING_RE = re.compile(
    r"^#{1,6}\s*(?:references|bibliography|citations|works cited|sources)\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# --- Candidate shapes ---
_CAP_PHRASE_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")
_PROPER_NOUN_RE = re.compile(r"(?<!\w)([A-Z][a-zA-Z]{2,})(?!\w)")
# Product/version tokens with internal dots, hyphens, or plus — ``Node.js``,
# ``TensorFlow.js``, ``GPT-4.1``. NER recognises these inconsistently, so this
# rule surfaces them from plain prose. The name-shape gate and the bare-
# lowercase rule still reject lowercase code (`note.created`, `v1.2.3`).
_PRODUCT_TOKEN_RE = re.compile(r"(?<![\w.])([A-Za-z][A-Za-z0-9]*(?:[.+-][A-Za-z0-9]+)+)(?!\w)")
_POSSESSIVE_RE = re.compile(r"'s\b")
_TRAILING_NUMERIC_RE = re.compile(r"^\d[\d.\-/:]*$")
# A semver-style release token: ``v1.2.3`` (v + at least one dot) or ``1.2.3``
# (three+ dotted parts). A candidate containing one is a release/version
# artifact, not a named entity — but simple product-version numbers like ``11``
# or ``3.5`` (``Windows 11``, ``Claude 3.5``, ``Python 3.11``) are NOT matched.
_VERSION_TOKEN_RE = re.compile(r"^(?:v\d+\.\d[\d.]*|\d+\.\d+\.\d[\d.]*)$")
# A document/config/data/script filename — its basename is not an entity,
# regardless of case (``README.md``, ``AGENTS.md``, ``settings.json``). ``.js``
# and ``.ts`` are deliberately excluded so ``Node.js`` / ``TensorFlow.js``
# survive.
_FILENAME_RE = re.compile(
    r"(?i)\.(?:md|markdown|json|ya?ml|toml|cfg|ini|lock|csv|tsv|txt|rst|"
    r"py|rb|go|rs|sh|bash|zsh|html?|xml|sql|png|jpe?g|gif|svg|pdf)$"
)
# Code-punctuation or LaTeX that disqualifies even an author-asserted wiki-link
# target (``[[\phi]]``, ``[[IntegerPropertyFilter(...)]]``).
_WIKI_JUNK_RE = re.compile(r"[()\[\]{}=<>\\]")
# A valid entity name: alphanumeric runs joined by spaces, hyphens, apostrophes,
# ampersands, dots, or plus/hash (for ``Node.js``, ``GPT-4.1``, ``C++``, ``C#``).
# Slashes, quotes, brackets, underscores, equals, and other operators are still
# rejected. The companion bare-lowercase rule in ``_clean_candidate`` rejects
# all-lowercase dotted code (`note.created`, `asyncio.gather`, `foo.md`) while
# keeping capitalized product names. This is the single gate every candidate
# passes through (#320).
_ENTITY_NAME_RE = re.compile(r"^[A-Za-z0-9]+(?:[ '&.+#-][A-Za-z0-9]*)*$")

# Sentence-like headings carry real entities ("# Festo launches ..."); short
# headings are template labels ("## Summary").
_SENTENCE_HEADING_MIN_WORDS = 4
_LABEL_MAX_WORDS = 3
_MAX_ENTITY_WORDS = 5
_MAX_ENTITY_CHARS = 60
_MIN_ENTITY_CHARS = 3
# Backstop against citation/glossary explosions: keep the most frequent
# entities when a single document yields more than this (#320).
_MAX_ENTITIES_PER_DOC = 50

_EDGE_STRIP = ".,;:!?\"'`()[]{}/\\—–- "  # noqa: RUF001

# Calendar words are capitalized by convention, not entity-hood. spaCy's
# STOP_WORDS (imported lazily below) covers ordinary function words (#174).
_CALENDAR_WORDS = frozenset(
    [
        "january", "february", "march", "april", "may", "june", "july",
        "august", "september", "october", "november", "december",
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
        "sunday", "today", "tomorrow", "yesterday",
    ]
)  # fmt: skip

_NLP: Language | None = None
_NER_UNAVAILABLE = False
_STOP_WORDS: frozenset[str] | None = None


def _load_model() -> Language:
    """Load the spaCy NER pipeline (separated for testability)."""
    import spacy

    return spacy.load(_MODEL_NAME, disable=["lemmatizer"])


def _get_nlp() -> Language | None:
    """Return the cached spaCy pipeline, or None when unavailable."""
    global _NLP, _NER_UNAVAILABLE
    if _NER_UNAVAILABLE:
        return None
    if _NLP is None:
        try:
            _NLP = _load_model()
        except Exception:
            logger.warning(
                "NER model %s unavailable; falling back to heuristic entity extraction",
                _MODEL_NAME,
                exc_info=True,
            )
            _NER_UNAVAILABLE = True
            return None
    return _NLP


def _stop_words() -> frozenset[str]:
    global _STOP_WORDS
    if _STOP_WORDS is None:
        from spacy.lang.en.stop_words import STOP_WORDS

        _STOP_WORDS = frozenset(STOP_WORDS)
    return _STOP_WORDS


def _is_noise_word(word: str) -> bool:
    lowered = word.lower()
    return lowered in _stop_words() or lowered in _CALENDAR_WORDS


def _clean_candidate(raw: str) -> str | None:
    """Normalize a candidate; return None when it cannot name an entity.

    The single validation gate for every extraction path. Strips edge
    punctuation and leading/trailing stop words, then requires the result to be
    name-shaped (``_ENTITY_NAME_RE``) — so code, filenames, quoted strings, and
    punctuation soup are rejected uniformly (#320).
    """
    text = _POSSESSIVE_RE.sub("", raw)
    words = text.strip().strip(_EDGE_STRIP).split()
    # A semver token anywhere (`v1.2.3`, `1.2.3`) marks the whole candidate as a
    # release artifact — e.g. NER's "Release v1.2.3" / "Upgrade 1.2.3". Reject
    # before the trailing-numeric strip would otherwise leave a bare verb.
    if any(_VERSION_TOKEN_RE.match(w) for w in words):
        return None
    while words and (_is_noise_word(words[0]) or words[0].isdigit()):
        words = words[1:]
    while words and (_is_noise_word(words[-1]) or _TRAILING_NUMERIC_RE.match(words[-1])):
        words = words[:-1]
    text = " ".join(words).strip("—–- ")  # noqa: RUF001
    if not (_MIN_ENTITY_CHARS <= len(text) <= _MAX_ENTITY_CHARS):
        return None
    if len(words) > _MAX_ENTITY_WORDS:
        return None
    if not _ENTITY_NAME_RE.match(text):
        return None
    # A filename basename is not an entity, whatever its case (`README.md`,
    # `settings.json`). `.js`/`.ts` are excluded so Node.js/TensorFlow.js survive.
    if _FILENAME_RE.search(text):
        return None
    # A bare lowercase single token is code/jargon (`node`, `task`, `guides`),
    # not a named entity — real names are capitalized or multi-word.
    if len(words) == 1 and text.islower():
        return None
    return text


def _mid_sentence_count(text: str, candidate: str) -> int:
    """Count occurrences not at a line/sentence/bullet/label start."""
    count = 0
    for match in re.finditer(rf"(?<!\w){re.escape(candidate)}(?!\w)", text):
        i = match.start() - 1
        while i >= 0 and text[i] in " \t":
            i -= 1
        if i >= 0 and (text[i].islower() or text[i] in ",;)"):
            count += 1
    return count


def _strip_reference_section(text: str) -> str:
    """Remove only the reference/bibliography *section*, not everything after it.

    Strips from the first references-style heading up to the next heading at the
    same or higher level (or end of document), so a later ``## Appendix`` or
    postscript survives (#320).
    """
    match = _REFERENCES_HEADING_RE.search(text)
    if not match:
        return text
    heading = match.group(0).lstrip()
    level = len(heading) - len(heading.lstrip("#"))
    for nxt in re.finditer(r"^(#{1,6})\s", text[match.end() :], re.MULTILINE):
        if len(nxt.group(1)) <= level:
            return text[: match.start()] + text[match.end() + nxt.start() :]
    return text[: match.start()]


def _structural_labels(text: str) -> set[str]:
    """Lowercased short heading/bold-label texts — template structure, not entities."""
    labels: set[str] = set()
    for source in (_HEADING_RE, _BOLD_LABEL_RE):
        for match in source.finditer(text):
            raw = match.group(2) if source is _BOLD_LABEL_RE else match.group(1)
            cleaned = _clean_candidate(raw)
            if cleaned and len(cleaned.split()) <= _LABEL_MAX_WORDS:
                labels.add(cleaned.lower())
    return labels


def _strip_structure(text: str) -> str:
    """Remove tables and bold-label prefixes (keeping label values)."""
    text = _TABLE_LINE_RE.sub("", text)
    return _BOLD_LABEL_RE.sub(lambda m: m.group(1), text)


def _ner_entities(text: str) -> set[str]:
    """Run NER over a cleaned view where sentence-like headings survive."""
    nlp = _get_nlp()
    if nlp is None:
        return set()

    def _heading_to_sentence(match: re.Match[str]) -> str:
        heading = match.group(1)
        if len(heading.split()) >= _SENTENCE_HEADING_MIN_WORDS:
            return heading.rstrip(".") + ".\n"
        return ""

    ner_input = _HEADING_RE.sub(_heading_to_sentence, text)
    ner_input = _EMPHASIS_RE.sub("", ner_input)

    found: set[str] = set()
    for ent in nlp(ner_input).ents:
        if ent.label_ not in _NER_LABELS or "\n" in ent.text:
            continue
        cleaned = _clean_candidate(ent.text)
        if cleaned:
            found.add(cleaned)
    return found


def _heuristic_entities(prose: str) -> set[str]:
    """Capitalized phrases / proper nouns / product tokens corroborated mid-sentence.

    A multi-word phrase or a punctuated product token (``Node.js``, ``GPT-4.1``)
    needs one mid-sentence occurrence; a bare single word needs two (a single
    word capitalized once mid-sentence is too weak a signal on its own).
    """
    candidates: set[str] = set()
    for match in _CAP_PHRASE_RE.finditer(prose):
        cleaned = _clean_candidate(match.group(1))
        if cleaned and " " in cleaned:
            candidates.add(cleaned)
    for match in _PRODUCT_TOKEN_RE.finditer(prose):
        cleaned = _clean_candidate(match.group(1))
        if cleaned:
            candidates.add(cleaned)
    for match in _PROPER_NOUN_RE.finditer(prose):
        word = match.group(1)
        if not _is_noise_word(word) and not word.isupper():
            cleaned = _clean_candidate(word)
            if cleaned:
                candidates.add(cleaned)

    confirmed: set[str] = set()
    for candidate in candidates:
        # Multi-word phrases and punctuated product tokens are distinctive
        # enough to trust on a single mid-sentence sighting; a bare word is not.
        distinctive = any(c in candidate for c in " .+-")
        required = 1 if distinctive else 2
        if _mid_sentence_count(prose, candidate) >= required:
            confirmed.add(candidate)
    return confirmed


_SEPARATORS = " .-+#"


def _drop_subsumed_singles(entities: set[str]) -> set[str]:
    """Drop a single token contained as a delimited part of a longer entity.

    Treats space, dot, hyphen, plus, and hash as token separators, so ``Node``
    is dropped when ``Node.js`` is kept, and ``Victor`` when ``Victor
    Stormbeard`` is kept.
    """
    containers = [e for e in entities if any(c in e for c in _SEPARATORS)]
    kept: set[str] = set()
    for entity in entities:
        is_single = not any(c in entity for c in _SEPARATORS)
        if is_single and any(
            re.search(rf"(?<!\w){re.escape(entity)}(?!\w)", c) for c in containers
        ):
            continue
        kept.add(entity)
    return kept


def _cap_entities(entities: set[str], forced: set[str], text: str, cap: int) -> set[str]:
    """Trim to ``cap`` entities by body frequency (#320).

    ``forced`` (author-asserted wiki-link targets) are always kept; the
    remaining slots go to the most frequently mentioned candidates, ties broken
    alphabetically for deterministic output. ``cap <= 0`` disables trimming.
    """
    if cap <= 0 or len(entities) <= cap:
        return entities
    kept = set(forced & entities)
    rest = entities - kept
    counts = {e: sum(1 for _ in re.finditer(rf"(?<!\w){re.escape(e)}(?!\w)", text)) for e in rest}
    for entity in sorted(rest, key=lambda e: (-counts[e], e)):
        if len(kept) >= cap:
            break
        kept.add(entity)
    return kept


def extract_entities(text: str, max_per_doc: int = _MAX_ENTITIES_PER_DOC) -> list[str]:
    """Extract entity names from note content.

    ``max_per_doc`` bounds the *derived* entities (NER + heuristic + backtick)
    to the most frequently mentioned; ``0`` disables it. Author-asserted
    wiki-link targets are always kept and are not counted against the bound, so
    a doc with many wiki-links can exceed it. Defaults to
    ``_MAX_ENTITIES_PER_DOC`` so callers without config still get the backstop.

    Returns a deduplicated, sorted list — deterministic so repeated
    extraction of unchanged content never churns frontmatter.
    """
    text = _CODE_FENCE_RE.sub("", text)
    # Drop the reference/bibliography section — citation author names are not
    # document entities — but keep any appendix that follows it (#320).
    text = _strip_reference_section(text)

    entities: set[str] = set()
    # Wiki-link targets are explicit author intent; kept verbatim (only edge
    # whitespace trimmed) and never dropped by the cap.
    wiki_targets: set[str] = set()
    for match in _WIKI_LINK_RE.finditer(text):
        target = match.group(1).strip()
        # Kept verbatim (author intent), but a target carrying code punctuation,
        # LaTeX, or a filename extension is not an entity.
        if target and not _WIKI_JUNK_RE.search(target) and not _FILENAME_RE.search(target):
            wiki_targets.add(target)
            entities.add(target)

    # Inline wiki-link targets so surrounding sentences stay parseable.
    no_links = _WIKI_LINK_RE.sub(lambda m: m.group(1), text)
    # Backtick terms pass the same name-shape gate as everything else, so inline
    # code (`merge_and_normalize()`, `note.created`, `"guides"`, `foo.md`) is
    # rejected rather than harvested as an entity.
    for match in _BACKTICK_RE.finditer(no_links):
        term = _clean_candidate(match.group(1))
        if term:
            entities.add(term)

    labels = _structural_labels(no_links)
    cleaned_text = _strip_structure(no_links)

    ner_candidates = _ner_entities(cleaned_text)

    prose = _HEADING_RE.sub("", cleaned_text)
    prose = _EMPHASIS_RE.sub("", prose)
    candidates = ner_candidates | _heuristic_entities(prose)

    for candidate in candidates:
        # A candidate matching a short heading/bold label is usually template
        # structure ("Summary", "Key Quotes") — but a note may legitimately
        # head a section with its subject ("## Lithos"). Demand independent
        # evidence: NER confirmation or two mid-sentence occurrences in prose.
        if (
            candidate.lower() in labels
            and candidate not in ner_candidates
            and _mid_sentence_count(prose, candidate) < 2
        ):
            continue
        entities.add(candidate)

    entities = _cap_entities(entities, wiki_targets, no_links, max_per_doc)
    return sorted(_drop_subsumed_singles(entities))
