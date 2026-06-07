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
# heading-harvesting heuristic extractor removed by #313.
ENTITY_EXTRACTOR_VERSION = 2

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

# --- Candidate shapes ---
_CAP_PHRASE_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")
_PROPER_NOUN_RE = re.compile(r"(?<!\w)([A-Z][a-zA-Z]{2,})(?!\w)")
_POSSESSIVE_RE = re.compile(r"'s\b")
_TRAILING_NUMERIC_RE = re.compile(r"^\d[\d.\-/:]*$")

# Sentence-like headings carry real entities ("# Festo launches ..."); short
# headings are template labels ("## Summary").
_SENTENCE_HEADING_MIN_WORDS = 4
_LABEL_MAX_WORDS = 3
_MAX_ENTITY_WORDS = 5
_MAX_ENTITY_CHARS = 60
_MIN_ENTITY_CHARS = 3

_BAD_CHARS = frozenset("<>|=&#@{}[]/\\")

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
    """Normalize a candidate; return None when it cannot name an entity."""
    text = _POSSESSIVE_RE.sub("", raw)
    words = text.strip().strip(".,;:!?\"'`()—–- ").split()  # noqa: RUF001
    while words and (_is_noise_word(words[0]) or words[0].isdigit()):
        words = words[1:]
    while words and (_is_noise_word(words[-1]) or _TRAILING_NUMERIC_RE.match(words[-1])):
        words = words[:-1]
    text = " ".join(words).strip("—–- ")  # noqa: RUF001
    if not (_MIN_ENTITY_CHARS <= len(text) <= _MAX_ENTITY_CHARS):
        return None
    if len(words) > _MAX_ENTITY_WORDS:
        return None
    if any(c in _BAD_CHARS for c in text):
        return None
    if text[0].isdigit():
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
    """Capitalized phrases/proper nouns corroborated mid-sentence.

    Phrases need one mid-sentence occurrence; single words need two (a single
    word capitalized once mid-sentence is too weak a signal on its own).
    """
    candidates: set[str] = set()
    for match in _CAP_PHRASE_RE.finditer(prose):
        cleaned = _clean_candidate(match.group(1))
        if cleaned and " " in cleaned:
            candidates.add(cleaned)
    for match in _PROPER_NOUN_RE.finditer(prose):
        word = match.group(1)
        if not _is_noise_word(word) and not word.isupper():
            cleaned = _clean_candidate(word)
            if cleaned:
                candidates.add(cleaned)

    confirmed: set[str] = set()
    for candidate in candidates:
        required = 1 if " " in candidate else 2
        if _mid_sentence_count(prose, candidate) >= required:
            confirmed.add(candidate)
    return confirmed


def _drop_subsumed_singles(entities: set[str]) -> set[str]:
    """Drop single words contained in a kept multi-word entity."""
    phrases = [e for e in entities if " " in e]
    kept: set[str] = set()
    for entity in entities:
        if " " not in entity and any(
            re.search(rf"(?<!\w){re.escape(entity)}(?!\w)", phrase) for phrase in phrases
        ):
            continue
        kept.add(entity)
    return kept


def extract_entities(text: str) -> list[str]:
    """Extract entity names from note content.

    Returns a deduplicated, sorted list — deterministic so repeated
    extraction of unchanged content never churns frontmatter.
    """
    text = _CODE_FENCE_RE.sub("", text)

    entities: set[str] = set()
    for match in _WIKI_LINK_RE.finditer(text):
        target = match.group(1).strip()
        if target:
            entities.add(target)

    # Inline wiki-link targets so surrounding sentences stay parseable.
    no_links = _WIKI_LINK_RE.sub(lambda m: m.group(1), text)
    for match in _BACKTICK_RE.finditer(no_links):
        term = match.group(1).strip()
        if term and len(term) <= _MAX_ENTITY_CHARS:
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
    return sorted(_drop_subsumed_singles(entities))
