"""Tests for lithos.lcma.entities — NER + heuristic entity extraction (#313, #174)."""

from __future__ import annotations

import logging

import pytest

import lithos.lcma.entities as entities_mod
from lithos.lcma.entities import ENTITY_EXTRACTOR_VERSION, extract_entities

# Abbreviated Influx article note — the rigid heading template that polluted
# every article note in prod/staging (issue #313).
INFLUX_ARTICLE = """\
# Festo launches lightweight pneumatic gripper and tests GripperAI

## Archive
path: rss/2026/06/the-robot-report-2026-06-03.html

## Summary
### Contributions
- Development of a compact, lightweight pneumatic gripper for collaborative robots
- Introduction of GripperAI, a robot-agnostic AI system enabling object grasping

### Method
Integration of lightweight pneumatic actuation with AI-driven grasping algorithms

### Result
Successful launch of a compact cobot gripper and demonstration of GripperAI's capability

### Relevance
Highly relevant to physical AI and dexterous manipulation, robot learning

## Profile Relevance
### robotics
Score: 7/10
The lightweight gripper with GripperAI relates to dexterous manipulation.

## User Notes
"""

INFLUX_JUNK = [
    "Summary",
    "Score",
    "User Notes",
    "Profile Relevance",
    "Method",
    "Result",
    "Introduces",
    "Demonstrates",
    "Highly",
    "Archive",
    "Contributions",
    "Relevance",
    "Relevance Highly",
    "Result Successful",
    "Successful",
    "Notes",
    "Profile",
    "User",
]


@pytest.fixture
def no_ner(monkeypatch: pytest.MonkeyPatch):
    """Force the heuristic fallback path (no spaCy model)."""
    monkeypatch.setattr(entities_mod, "_get_nlp", lambda: None)


class TestAuthorAssertedSignals:
    """Wiki-links and backtick terms are author-asserted and always extracted."""

    def test_wiki_links_extracted(self) -> None:
        entities = extract_entities("This references [[Knowledge Graph]] and [[NetworkX]].")
        assert "Knowledge Graph" in entities
        assert "NetworkX" in entities

    def test_wiki_link_with_display_text(self) -> None:
        entities = extract_entities("See [[target-doc|display name]] for details.")
        assert "target-doc" in entities

    def test_backtick_terms_extracted(self) -> None:
        entities = extract_entities("The `EnrichWorker` processes events from the `EventBus`.")
        assert "EnrichWorker" in entities
        assert "EventBus" in entities

    def test_code_fences_excluded(self) -> None:
        text = "Prose mentions nothing.\n```python\nclass MyInternalClass:\n    pass\n```\n"
        assert "MyInternalClass" not in extract_entities(text)

    def test_empty_text(self) -> None:
        assert extract_entities("") == []

    def test_deduplicated_and_sorted(self) -> None:
        entities = extract_entities("[[Alpha]] appears twice: [[Alpha]] and `Beta`.")
        assert entities == sorted(set(entities))


class TestInfluxTemplateRegression:
    """Issue #313: template headings and sentence-initial words must not survive."""

    def test_junk_absent(self) -> None:
        entities = extract_entities(INFLUX_ARTICLE)
        leaked = [e for e in entities if e in INFLUX_JUNK]
        assert leaked == [], f"template junk leaked into entities: {leaked}"

    def test_real_entities_survive(self) -> None:
        entities = extract_entities(INFLUX_ARTICLE)
        assert "GripperAI" in entities
        assert "Festo" in entities

    def test_junk_absent_in_fallback(self, no_ner: None) -> None:
        entities = extract_entities(INFLUX_ARTICLE)
        leaked = [e for e in entities if e in INFLUX_JUNK]
        assert leaked == [], f"template junk leaked (fallback path): {leaked}"
        # Fallback still catches the corroborated proper noun.
        assert "GripperAI" in entities


class TestJunkClasses:
    """Each junk class found in the prod corpus audit (2026-06-07)."""

    def test_heading_labels_not_extracted(self, no_ner: None) -> None:
        text = "## Key Points\n\nSome plain prose about nothing in particular.\n"
        entities = extract_entities(text)
        assert "Key Points" not in entities
        assert "Key" not in entities
        assert "Points" not in entities

    def test_heading_matching_real_entity_survives(self, no_ner: None) -> None:
        # A section headed by its subject must not suppress the entity when
        # prose independently corroborates it.
        text = "## Lithos\n\nWe use Lithos across the team. Everyone likes Lithos.\n"
        assert "Lithos" in extract_entities(text)

    def test_heading_matching_weak_mention_still_suppressed(self, no_ner: None) -> None:
        # One mid-sentence mention of a heading label is not enough evidence —
        # "see the Key Quotes section" style references stay structural.
        text = "## Key Quotes\n\nDetails are in the Key Quotes section above somewhere.\n"
        assert "Key Quotes" not in extract_entities(text)

    def test_heading_adjacent_runs_not_joined(self, no_ner: None) -> None:
        # "## Summary" followed by a capitalized sentence must not produce
        # "Summary Broadridge" style run-ons.
        text = "## Summary\nBroadridge announced new capabilities for the broadridge platform.\n"
        entities = extract_entities(text)
        assert not any(e.startswith("Summary") for e in entities)

    def test_sentence_initial_verbs_excluded(self, no_ner: None) -> None:
        text = (
            "Introduces a new method.\nDemonstrates the approach.\n"
            "Presents results.\nIdentifies gaps.\nShares data.\n"
        )
        entities = extract_entities(text)
        for junk in ["Introduces", "Demonstrates", "Presents", "Identifies", "Shares"]:
            assert junk not in entities

    def test_bold_label_lines_excluded(self, no_ner: None) -> None:
        text = "**Developer:** KD Software\n**Platform:** something\n**Genre:** adventure\n"
        entities = extract_entities(text)
        assert "Developer" not in entities
        assert "Platform" not in entities
        assert "Genre" not in entities

    def test_table_cells_excluded(self, no_ner: None) -> None:
        text = "| Feature | Detail |\n|---------|--------|\n| **Level** | Complete first level |\n"
        entities = extract_entities(text)
        for junk in ["Feature", "Detail", "Level", "Complete"]:
            assert junk not in entities

    def test_day_and_month_names_excluded(self, no_ner: None) -> None:
        text = "It happened in April, again on Thursday, and the demo of April was in June.\n"
        entities = extract_entities(text)
        assert "April" not in entities
        assert "Thursday" not in entities
        assert "June" not in entities

    def test_singles_subsumed_by_phrases(self, no_ner: None) -> None:
        text = (
            "The game stars Victor Stormbeard on his quest. People say Victor "
            "Stormbeard is brave, and players love Victor Stormbeard.\n"
        )
        entities = extract_entities(text)
        assert "Victor Stormbeard" in entities
        assert "Victor" not in entities
        assert "Stormbeard" not in entities

    def test_possessive_stripped(self, no_ner: None) -> None:
        text = "We rely on Broadridge's platform; the broadridge stack is Broadridge's pride.\n"
        entities = extract_entities(text)
        assert not any("'" in e for e in entities)

    def test_overlong_phrases_rejected(self, no_ner: None) -> None:
        text = (
            "# Broadridge Puts Agentic AI Into Production For Financial Operations\n\n"
            "Plain prose follows here.\n"
        )
        entities = extract_entities(text)
        assert all(len(e.split()) <= 5 for e in entities)

    def test_uncorroborated_sentence_initial_word_excluded(self, no_ner: None) -> None:
        # A capitalized word appearing only at sentence start is sentence case,
        # not a proper noun.
        text = "Successful launches were rare. Successful teams iterate.\n"
        assert "Successful" not in extract_entities(text)

    def test_corroborated_proper_noun_included(self, no_ner: None) -> None:
        # Mid-sentence capitalization is the classic NER casing signal; singles
        # need it twice.
        text = "We deployed Lithos today. Everyone praised Lithos, and the team adopted Lithos.\n"
        assert "Lithos" in extract_entities(text)


class TestNerRecall:
    """Cases the positional heuristics cannot rescue — NER must catch them."""

    def test_mid_sentence_org(self) -> None:
        text = "The contract was won by Broadridge Financial Solutions in May.\n"
        assert "Broadridge Financial Solutions" in extract_entities(text)

    def test_sentence_initial_org_in_title_heading(self) -> None:
        # "Festo" only ever appears sentence-initial in the article template;
        # heuristics drop it, NER on the sentence-like heading recovers it.
        text = "# Festo launches lightweight pneumatic gripper and tests GripperAI\n\nProse.\n"
        assert "Festo" in extract_entities(text)


class TestHeuristicFallback:
    """Model-unavailable behavior: degraded but high-precision extraction."""

    def test_fallback_warns_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _boom(*args: object, **kwargs: object) -> None:
            raise OSError("model not found")

        monkeypatch.setattr(entities_mod, "_load_model", _boom)
        monkeypatch.setattr(entities_mod, "_NLP", None)
        monkeypatch.setattr(entities_mod, "_NER_UNAVAILABLE", False)

        with caplog.at_level(logging.WARNING, logger="lithos.lcma.entities"):
            extract_entities("Some text mentioning [[Lithos]].")
            extract_entities("More text mentioning [[Lithos]].")

        warnings = [r for r in caplog.records if "NER model" in r.message]
        assert len(warnings) == 1

    def test_fallback_keeps_author_asserted(self, no_ner: None) -> None:
        entities = extract_entities("Uses [[NetworkX]] and `ChromaDB` heavily.")
        assert "NetworkX" in entities
        assert "ChromaDB" in entities


class TestExtractorVersion:
    def test_version_is_positive_int(self) -> None:
        assert isinstance(ENTITY_EXTRACTOR_VERSION, int)
        assert ENTITY_EXTRACTOR_VERSION >= 2
