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


class TestBacktickCodeRejection:
    """Backtick spans in technical docs are code, not entities (#320)."""

    @pytest.mark.parametrize(
        "code",
        [
            "`merge_and_normalize()`",
            "`min(0.1, days × 0.005)`",  # noqa: RUF001
            "`asyncio.gather`",
            "`surface_conflicts=True`",
            "`note.created`",
            "`scout_freshness`",
            "`decay_inactive_days`",
            '`["idea", "project:<slug>"]`',
            "`^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$`",
            "`temperature = 1 - coherence`",
        ],
    )
    def test_code_span_not_extracted(self, no_ner: None, code: str) -> None:
        text = f"The function {code} does something useful in the pipeline today.\n"
        entities = extract_entities(text)
        # Nothing code-shaped should survive.
        assert all("(" not in e and "_" not in e and "=" not in e for e in entities)
        assert all("." not in e and "[" not in e and '"' not in e for e in entities)

    def test_quoted_string_rejected(self, no_ner: None) -> None:
        text = 'Set the namespace to `"guides"` or `"influx"` as needed in config.\n'
        entities = extract_entities(text)
        assert not any('"' in e for e in entities)
        assert "guides" not in entities  # bare lowercase code value

    def test_filename_rejected(self, no_ner: None) -> None:
        text = "Edit `forms.md` and `plugin.json` then commit the changes to disk.\n"
        entities = extract_entities(text)
        assert "forms.md" not in entities
        assert "plugin.json" not in entities
        assert not any(e.endswith(".md") or e.endswith(".json") for e in entities)

    def test_trailing_slash_and_measurement_rejected(self, no_ner: None) -> None:
        text = "Put files under `references/` — each is roughly `~1,000 tokens` long here.\n"
        entities = extract_entities(text)
        assert "references/" not in entities
        assert not any("/" in e for e in entities)
        assert not any("~" in e or "," in e for e in entities)

    def test_name_shaped_backticks_survive(self, no_ner: None) -> None:
        text = "We use `Tantivy` and `ChromaDB`; the `StatsStore` holds salience data.\n"
        entities = extract_entities(text)
        assert "Tantivy" in entities
        assert "ChromaDB" in entities
        assert "StatsStore" in entities

    @pytest.mark.parametrize(
        "name",
        ["Node.js", "TensorFlow.js", "GPT-4.1", "C++"],  # C#/Go fall below the 3-char floor
    )
    def test_dotted_and_symbol_product_names_kept(self, no_ner: None, name: str) -> None:
        # Capitalized product names with dots/symbols are real entities, not
        # code — they must survive (regression for the v3 name-shape gate).
        text = f"We adopted `{name}` last year and `{name}` is still our stack.\n"
        assert name in extract_entities(text)

    @pytest.mark.parametrize("name", ["Node.js", "TensorFlow.js", "GPT-4.1"])
    def test_dotted_product_names_surface_in_prose(self, no_ner: None, name: str) -> None:
        # Plain prose (no backticks, no NER) must still surface these via the
        # product-token rule — the gap from the follow-up review.
        text = f"{name} is popular. We rely on {name} for production.\n"
        assert name in extract_entities(text)

    def test_lowercase_version_string_rejected(self, no_ner: None) -> None:
        text = "Release v1.2.3 today. We tagged v1.2.3 and shipped v1.2.3 widely.\n"
        assert "v1.2.3" not in extract_entities(text)


class TestVersionTokenRejection:
    """Verb+semver phrases (NER's "Release v1.2.3") are not entities.

    The junk originates from nondeterministic NER, so the gate is tested
    directly rather than through a full extract.
    """

    @pytest.mark.parametrize(
        "candidate", ["Release v1.2.3", "Lithos v2.0.1", "Upgrade 1.2.3", "Ship v1.2"]
    )
    def test_semver_phrase_rejected(self, candidate: str) -> None:
        assert entities_mod._clean_candidate(candidate) is None

    @pytest.mark.parametrize(
        ("candidate", "expected"),
        [
            ("Windows 11", "Windows"),  # simple version stripped, name kept
            ("Claude 3.5", "Claude"),
            ("Python 3.11", "Python"),
            ("GPT-4.1", "GPT-4.1"),  # single product token, not a semver phrase
        ],
    )
    def test_simple_product_versions_kept(self, candidate: str, expected: str) -> None:
        assert entities_mod._clean_candidate(candidate) == expected

    @pytest.mark.parametrize(
        ("token", "is_version"),
        [
            ("v1.2.3", True),
            ("1.2.3", True),
            ("v1.2", True),
            ("11", False),
            ("3.5", False),
            ("3.11", False),
            ("2024", False),
        ],
    )
    def test_version_token_contract(self, token: str, is_version: bool) -> None:
        assert bool(entities_mod._VERSION_TOKEN_RE.match(token)) is is_version

    def test_lowercase_dotted_code_still_rejected(self, no_ner: None) -> None:
        # The dot allowance must not let lowercase code/attributes/filenames in.
        text = "Watch `note.created`, call `asyncio.gather`, edit `metadata.project` today.\n"
        entities = extract_entities(text)
        for junk in ["note.created", "asyncio.gather", "metadata.project"]:
            assert junk not in entities

    def test_dotted_name_subsumes_bare_prefix(self, no_ner: None) -> None:
        text = "We run `Node.js` here. The `Node.js` runtime scales; `Node.js` again.\n"
        entities = extract_entities(text)
        assert "Node.js" in entities
        assert "Node" not in entities

    def test_bare_lowercase_word_rejected(self, no_ner: None) -> None:
        text = "A `node` has a `task`; we `update` and `verify` it during the run.\n"
        entities = extract_entities(text)
        for junk in ["node", "task", "update", "verify"]:
            assert junk not in entities


class TestFilenameRejection:
    """Filenames are never entities, regardless of case (#320 v4)."""

    @pytest.mark.parametrize(
        "filename",
        ["README.md", "AGENTS.md", "CLAUDE.md", "settings.json", "config.yaml", "script.py"],
    )
    def test_filename_rejected(self, no_ner: None, filename: str) -> None:
        text = f"The {filename} file matters. We read {filename} and edit {filename} often.\n"
        assert filename not in extract_entities(text)

    @pytest.mark.parametrize("product", ["Node.js", "TensorFlow.js"])
    def test_js_product_names_not_treated_as_filenames(self, no_ner: None, product: str) -> None:
        text = f"{product} is our stack. We rely on {product} and ship {product}.\n"
        assert product in extract_entities(text)


class TestWikiLinkGuard:
    """Wiki-link targets are author-asserted but still reject obvious junk."""

    def test_code_and_latex_targets_dropped(self) -> None:
        text = "See [[\\phi]] and [[IntegerPropertyFilter(x=1)]] and [[Knowledge Graph]] here.\n"
        entities = extract_entities(text)
        assert "Knowledge Graph" in entities
        assert "\\phi" not in entities
        assert not any("(" in e for e in entities)

    def test_filename_wiki_target_dropped(self) -> None:
        entities = extract_entities("Refer to [[README.md]] and [[Knowledge Graph]] now.\n")
        assert "README.md" not in entities
        assert "Knowledge Graph" in entities

    def test_normal_wiki_links_unaffected(self) -> None:
        entities = extract_entities("Links to [[NetworkX]] and [[target-doc|display]] here.\n")
        assert "NetworkX" in entities
        assert "target-doc" in entities

    @pytest.mark.parametrize(
        "title",
        [
            "Mercury (planet)",
            "Dune (novel)",
            "C (programming language)",
            "The C Programming Language (book)",
        ],
    )
    def test_parenthetical_disambiguation_titles_kept(self, title: str) -> None:
        # Disambiguated note titles are a common wiki pattern — a space before
        # the paren marks disambiguation, not a function call.
        assert title in extract_entities(f"See [[{title}]] for context here.\n")

    def test_function_call_wiki_target_still_dropped(self) -> None:
        # name( with no space is a function call, not disambiguation.
        entities = extract_entities("See [[parse(x)]] and [[Knowledge Graph]] now.\n")
        assert "Knowledge Graph" in entities
        assert not any("(" in e for e in entities)


class TestReferenceSectionStripping:
    """Citation/bibliography sections are author-name soup, not entities (#320)."""

    def test_references_section_excluded(self) -> None:
        text = (
            "# Neural Algorithmic Reasoning\n\n"
            "This work studies graph algorithms with Petar Velickovic at DeepMind.\n\n"
            "## References\n\n"
            "- Battaglia, P. (2018). Relational inductive biases.\n"
            "- Colmenarejo, S. (2020). Some other cited paper here.\n"
            "- Cormen, T. (2009). Introduction to Algorithms.\n"
        )
        entities = extract_entities(text)
        # Authors cited only in the reference list must not appear.
        assert "Battaglia" not in entities
        assert "Colmenarejo" not in entities
        assert "Cormen" not in entities

    def test_appendix_after_references_preserved(self) -> None:
        # Only the reference section is stripped — a following same-level
        # section survives.
        text = (
            "# Doc\n\nIntro mentioning Anthropic in prose.\n\n"
            "## References\n\n- Smith, J. (2020). A cited paper here.\n\n"
            "## Appendix\n\nOpenAI shipped a model. We tested OpenAI; OpenAI again.\n"
        )
        entities = extract_entities(text)
        assert "OpenAI" in entities
        assert "Smith" not in entities  # still excluded — inside references

    def test_deeper_subsections_under_references_stripped(self) -> None:
        text = (
            "# Doc\n\nIntro about Anthropic in the body.\n\n"
            "## References\n\n### Primary\n- Battaglia, P. (2018). Paper.\n\n"
            "### Secondary\n- Cormen, T. (2009). Book.\n\n"
            "## Conclusion\n\nDeepMind is mentioned here. We cite DeepMind, then DeepMind.\n"
        )
        entities = extract_entities(text)
        assert "Battaglia" not in entities
        assert "Cormen" not in entities
        assert "DeepMind" in entities  # conclusion after the whole ref section


class TestPerDocCap:
    """The per-doc cap trims by frequency, keeps forced, is configurable (#320)."""

    def test_cap_trims_to_limit(self) -> None:
        ents = {f"Name{i}" for i in range(80)}
        text = " ".join(f"Name{i} appears." for i in range(80))
        assert len(entities_mod._cap_entities(ents, set(), text, 10)) == 10

    def test_cap_keeps_most_frequent(self) -> None:
        ents = {"Rare", "Common", "Mid"}
        text = "Common Common Common. Mid Mid. Rare."
        kept = entities_mod._cap_entities(ents, set(), text, 2)
        assert kept == {"Common", "Mid"}

    def test_forced_always_kept(self) -> None:
        ents = {"Wiki", "Common", "Mid"}
        text = "Common Common. Mid Mid. Wiki."
        kept = entities_mod._cap_entities(ents, {"Wiki"}, text, 1)
        assert "Wiki" in kept and len(kept) == 1

    def test_cap_zero_disables(self) -> None:
        ents = {f"Name{i}" for i in range(80)}
        assert entities_mod._cap_entities(ents, set(), "", 0) == ents

    def test_cap_under_limit_unchanged(self) -> None:
        ents = {"A", "B", "C"}
        assert entities_mod._cap_entities(ents, set(), "", 50) == ents

    def test_max_per_doc_param_threads_through(self) -> None:
        # Wiki targets are forced, so a tiny cap still keeps all of them —
        # this confirms the param reaches _cap_entities without relying on the
        # NER/heuristic path producing a deterministic count.
        text = "".join(f"[[Pinned {i}]] " for i in range(60))
        assert len(extract_entities(text, max_per_doc=5)) == 60


class TestExtractorVersion:
    def test_version_is_positive_int(self) -> None:
        assert isinstance(ENTITY_EXTRACTOR_VERSION, int)
        assert ENTITY_EXTRACTOR_VERSION >= 3
