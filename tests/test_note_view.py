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
        text = render_note(_view(), limit=1200)
        assert "8/10" not in text and "rss/2026" not in text and "# A paper" not in text

    def test_method_only_when_it_fits_whole(self) -> None:
        head = render_note(_view(), limit=1200).split("Method:")[0].rstrip("\n")
        text = render_note(_view(), limit=len(head) + 10, chunk=None)
        assert text == head

    def test_chunk_left_out_when_the_summary_already_shows_it(self) -> None:
        chunk = "## Summary\n### Contributions\n- First contribution\n- Second contribution"
        assert "Matching passage" not in render_note(_view(), limit=1200, chunk=chunk)

    def test_chunk_left_out_without_room(self) -> None:
        body = render_note(_view(), limit=1200)
        assert render_note(_view(), limit=len(body) + 60, chunk=CHUNK) == body

    def test_text_is_cut_at_the_limit(self) -> None:
        assert len(render_note(_view(content="word " * 1000), limit=300)) == 300

    def test_a_note_without_summary_shows_its_body_without_title_or_archive(self) -> None:
        content = "# A paper\n\n## Archive\npath: x.html\n\n## Notes\nPlain body text.\n"
        assert render_note(_view(content=content), limit=1200) == "## Notes\nPlain body text."
