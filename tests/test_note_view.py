"""What the adjudicator sees of one note (prompt v2): :mod:`lithos.lcma.note_view`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from lithos.frontmatter_codec import KnowledgeDocument, KnowledgeMetadata
from lithos.lcma.note_view import (
    STORED_CHARS,
    NoteView,
    provenance_line,
    render_note,
    usable_content,
    view_of,
)

INFLUX_BODY = """# A paper

## Archive
path: rss/2026/10/feed-2026-10-15-abc.html

## Summary
### Contributions
- First contribution
- Second contribution

### Method
A short method.

### Result
It worked.

### Relevance
8/10 for the robotics profile.

## Full Text
The whole article, which the view never keeps.
"""

CHUNK = "A passage from the article body that matched the focus note, long enough to show."


def _view(content: str = INFLUX_BODY, **kwargs: str) -> NoteView:
    return NoteView(title="A paper", content=content, **kwargs)


class TestViewOf:
    def test_keeps_summary_date_source_and_feed(self) -> None:
        metadata = KnowledgeMetadata(
            id="n1",
            title="A paper",
            author="influx",
            created_at=datetime(2026, 10, 15, 16, 39, tzinfo=UTC),
            updated_at=datetime(2026, 10, 16, tzinfo=UTC),
            tags=["profile:robotics", "feed-slug:robot-news", "source:rss"],
            source_url="https://www.example.org/post/1",
        )
        doc = KnowledgeDocument(
            id="n1", title="A paper", content=INFLUX_BODY, metadata=metadata, path=Path("n1.md")
        )
        view = view_of(doc)
        assert view.created_at == "2026-10-15T16:39:00+00:00"
        assert view.source_url == "https://www.example.org/post/1"
        assert view.feed == "robot-news"
        assert "### Relevance" in view.content  # kept: the summary ends at Full Text
        assert "Full Text" not in view.content

    def test_round_trips_through_a_dict_without_empty_fields(self) -> None:
        view = _view(created_at="2026-10-15T00:00:00+00:00")
        assert view.to_dict() == {
            "title": "A paper",
            "content": INFLUX_BODY,
            "created_at": "2026-10-15T00:00:00+00:00",
        }
        assert NoteView.from_dict(view.to_dict()) == view


class TestUsableContent:
    def test_a_note_without_summary_keeps_its_start(self) -> None:
        assert usable_content("x" * (STORED_CHARS + 50)) == "x" * STORED_CHARS


class TestProvenanceLine:
    def test_variants(self) -> None:
        assert provenance_line(_view()) == ""
        assert provenance_line(_view(created_at="2026-10-15T16:39:00+00:00")) == "Added 2026-10-15"
        assert provenance_line(_view(source_url="https://www.example.org/x")) == (
            "Source: example.org"
        )
        assert provenance_line(_view(feed="robot-news")) == "Source: robot-news"
        assert provenance_line(
            _view(
                created_at="2026-10-15T16:39:00+00:00",
                source_url="https://arxiv.org/abs/2610.00001",
                feed="arxiv-cs-ro",
            )
        ) == ("Added 2026-10-15 · Source: arxiv.org (arxiv-cs-ro)")


class TestRenderNote:
    def test_summary_head_then_method_then_chunk(self) -> None:
        text = render_note(_view(created_at="2026-10-15T00:00:00+00:00"), limit=1200, chunk=CHUNK)
        assert text == (
            "Added 2026-10-15\n"
            "Contributions:\n- First contribution\n- Second contribution\n"
            "Result:\nIt worked.\n"
            "Method:\nA short method.\n"
            f"Matching passage: {CHUNK}"
        )

    def test_relevance_archive_and_title_are_never_shown(self) -> None:
        text = render_note(_view(), limit=1200, chunk=INFLUX_BODY)
        assert "8/10" not in text and "rss/2026" not in text and "# A paper" not in text

    def test_method_only_when_it_fits_whole(self) -> None:
        head = render_note(_view(), limit=1200).split("Method:")[0].rstrip("\n")
        text = render_note(_view(), limit=len(head) + 10, chunk=None)
        assert text == head

    def test_chunk_left_out_when_the_summary_already_shows_it(self) -> None:
        chunk = "## Summary\n### Contributions\n- First contribution\n- Second contribution"
        assert "Matching passage" not in render_note(_view(), limit=1200, chunk=chunk)

    def test_chunk_drops_lines_already_shown_whatever_their_order(self) -> None:
        """PR #462 review follow-up: a chunk holding the summary in the note's own
        order (Method before Result) is no new material."""
        chunk = (
            "## Summary\n### Contributions\n- First contribution\n- Second contribution\n\n"
            "### Method\nA short method.\n\n### Result\nIt worked.\n\n## User Notes"
        )
        assert "Matching passage" not in render_note(_view(), limit=1200, chunk=chunk)
        with_new = chunk.replace("## User Notes", "## Full Text\nA new finding.")
        assert render_note(_view(), limit=1200, chunk=with_new).endswith(
            "Matching passage: ## Full Text\nA new finding."
        )

    def test_chunk_left_out_without_room(self) -> None:
        body = render_note(_view(), limit=1200)
        assert render_note(_view(), limit=len(body) + 60, chunk=CHUNK) == body

    def test_excluded_sections_never_reach_the_prompt_through_the_chunk(self) -> None:
        """PR #462 review: a relevance-only chunk (397f893f3a105217/5) leaked the
        robotics profile score into the prompt as a matching passage."""
        relevance_chunk = (
            "### Relevance\n"
            "Highly relevant to robot learning from demonstration and sim-to-real transfer.\n"
            "\n"
            "## Profile Relevance\n"
            "### robotics\n"
            "Score: 7/10\n"
            "The interactive world simulator is relevant to robot learning."
        )
        text = render_note(_view(), limit=1200, chunk=relevance_chunk)
        assert "Matching passage" not in text
        assert "Score: 7/10" not in text and "Highly relevant" not in text

    def test_a_profile_section_without_its_parent_heading_is_excluded(self) -> None:
        """A chunk can start at a later per-profile section (0fb7fe512b9666c4/3)."""
        chunk = (
            "### ai-agents\nScore: 9/10\nHighly relevant to agentic frameworks and tool use.\n\n"
            "### knowledge-systems\nScore: 6.5/10\nSome relevance to shared memory."
        )
        assert "Matching passage" not in render_note(_view(), limit=1200, chunk=chunk)

    def test_chunk_keeps_its_content_without_title_archive_or_relevance(self) -> None:
        first_chunk = (
            "A paper\n\n# A paper\n\n## Archive\npath: rss/2026/10/feed-2026-10-15-abc.html\n\n"
            "## Full Text\nThe article's first paragraph.\n\n"
            "## Profile Relevance\n### robotics\nScore: 7/10\n\n## User Notes"
        )
        text = render_note(_view(content=""), limit=1200, chunk=first_chunk)
        assert text == "Matching passage: ## Full Text\nThe article's first paragraph."

    def test_chunk_is_shown_alone_within_a_small_limit(self) -> None:
        """PR #462 review: with no body, the chunk is the fallback whatever the room."""
        view = NoteView(title="Candidate", content="")
        chunk = "Concrete evidence from the candidate note."
        assert render_note(view, limit=100, chunk=chunk) == f"Matching passage: {chunk}"
        assert len(render_note(view, limit=40, chunk=chunk)) == 40

    def test_text_is_cut_at_the_limit(self) -> None:
        assert len(render_note(_view(content="word " * 1000), limit=300)) == 300

    def test_a_note_without_summary_shows_its_body_without_title_or_archive(self) -> None:
        content = "# A paper\n\n## Archive\npath: x.html\n\n## Notes\nPlain body text.\n"
        assert render_note(_view(content=content), limit=1200) == "## Notes\nPlain body text."
