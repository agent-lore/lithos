"""What the typed-edge adjudicator sees of one note (prompt v2).

Prompt v1 showed the focus note's first characters and, for each candidate,
the one search chunk that best matched the focus. On the 2026-11 golden set
that chunk was often Influx's relevance blurb rather than the note's content,
and only one candidate in ten showed a date or source, so the model could not
tell which of two near-duplicates came first.

v2 shows every note the same way: a line with the date it was added and its
source, then the head of its summary, then (for a candidate) the matching chunk
if there is room and the summary does not already contain it. The functions
here are pure, so the engine, the eval runner and the labelling sheet render
identical text from the same :class:`NoteView`.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from lithos.frontmatter_codec import KnowledgeDocument

#: Summary subsections always shown, in order, then the one shown only when it
#: fits whole. Influx's ``Relevance`` is its view of the note's fit to a
#: profile, not the note's content, so it is left out.
SUMMARY_SECTIONS = ("Contributions", "Result")
OPTIONAL_SECTION = "Method"

#: Most of a note's text a view keeps: enough for any summary head, small
#: enough that a logged request stays a few kilobytes per note.
STORED_CHARS = 3000

FEED_TAG_PREFIX = "feed-slug:"

#: A matching chunk is only worth adding when this much room is left.
_MIN_CHUNK_ROOM = 120

_H2 = re.compile(r"^## +(.+?)[ \t]*$", re.MULTILINE)
_H3 = re.compile(r"^### +(.+?)[ \t]*$", re.MULTILINE)
_HEADING_LINE = re.compile(r"^#{1,6} ")


@dataclass(frozen=True)
class NoteView:
    """The parts of a note the adjudication prompt can show."""

    title: str
    content: str
    created_at: str | None = None
    source_url: str | None = None
    feed: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NoteView:
        return cls(
            title=str(data.get("title", "")),
            content=str(data.get("content", "")),
            created_at=_optional(data.get("created_at")),
            source_url=_optional(data.get("source_url")),
            feed=_optional(data.get("feed")),
        )


def view_of(doc: KnowledgeDocument) -> NoteView:
    """The view of a stored note: its usable text plus date, source and feed."""
    feeds = [t[len(FEED_TAG_PREFIX) :] for t in doc.metadata.tags if t.startswith(FEED_TAG_PREFIX)]
    return NoteView(
        title=doc.title,
        content=usable_content(doc.content),
        created_at=doc.metadata.created_at.isoformat(),
        source_url=doc.metadata.source_url or None,
        feed=feeds[0] if feeds else None,
    )


def usable_content(content: str) -> str:
    """The part of a note's body a view can show.

    For a note with a ``## Summary`` section that is everything up to the end
    of the summary (full text and extraction sections follow it); otherwise the
    start of the body. Either way at most :data:`STORED_CHARS` characters.
    """
    summary = _section(content, _H2, "Summary")
    if summary is not None:
        content = content[: summary[2]]
    return content[:STORED_CHARS]


def provenance_line(view: NoteView) -> str:
    """``Added 2026-10-15 · Source: example.org (feed)``, or ``""`` when unknown."""
    parts = []
    if view.created_at:
        parts.append(f"Added {view.created_at[:10]}")
    source = _domain(view.source_url) if view.source_url else ""
    if source and view.feed:
        parts.append(f"Source: {source} ({view.feed})")
    elif source or view.feed:
        parts.append(f"Source: {source or view.feed}")
    return " · ".join(parts)


def render_note(view: NoteView, *, limit: int, chunk: str | None = None) -> str:
    """The text shown for *view*, at most *limit* characters (title excluded).

    In order: the provenance line, the summary head (or the start of the body
    for a note without a summary), ``Method`` when it fits whole, and *chunk*
    (the search chunk that matched the focus) when at least
    :data:`_MIN_CHUNK_ROOM` characters remain and the text does not already
    contain it.
    """
    core, optional = _body(view)
    text = _join(provenance_line(view), core)
    if optional and len(text) + 1 + len(optional) <= limit:
        text = _join(text, optional)
    if chunk and chunk.strip() and not _already_shown(chunk, text):
        passage = f"Matching passage: {chunk.strip()}"
        if limit - len(text) - 1 >= _MIN_CHUNK_ROOM:
            text = _join(text, passage)
    return text[:limit]


def _body(view: NoteView) -> tuple[str, str]:
    """``(core, optional)``: the summary head and ``Method``, or the plain body and ``""``."""
    summary = _section(view.content, _H2, "Summary")
    if summary is not None:
        summary_text = view.content[summary[1] : summary[2]]
        core = _join(*(_subsection(summary_text, name) for name in SUMMARY_SECTIONS))
        optional = _subsection(summary_text, OPTIONAL_SECTION)
        if core or optional:
            return (core, optional) if core else (optional, "")
    return _plain(view.content, view.title), ""


def _subsection(summary_text: str, name: str) -> str:
    span = _section(summary_text, _H3, name)
    body = summary_text[span[1] : span[2]].strip() if span is not None else ""
    return f"{name}:\n{body}" if body else ""


def _join(*parts: str) -> str:
    return "\n".join(p for p in parts if p)


def _plain(content: str, title: str) -> str:
    """Body text with the title heading(s) and Influx's archive section dropped."""
    archive = _section(content, _H2, "Archive")
    if archive is not None:
        content = content[: archive[0]] + content[archive[2] :]
    lines = [line for line in content.splitlines() if line.strip() != f"# {title}"]
    return "\n".join(lines).strip()


def _section(text: str, heading: re.Pattern[str], name: str) -> tuple[int, int, int] | None:
    """Section *name* as ``(heading start, body start, end)``.

    The section ends at the next heading of the same level, or the end of *text*.
    """
    matches = list(heading.finditer(text))
    for i, m in enumerate(matches):
        if m.group(1).strip().lower() == name.lower():
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            return m.start(), m.end(), end
    return None


def _already_shown(chunk: str, text: str) -> bool:
    """Whether the start of *chunk*'s prose already appears in *text*."""
    probe = _prose(chunk)[:80]
    return bool(probe) and probe in _prose(text)


def _prose(text: str) -> str:
    """*text* without heading lines or list markers, whitespace collapsed."""
    lines = (line.strip() for line in text.splitlines())
    kept = (line.lstrip("-* ").strip() for line in lines if line and not _HEADING_LINE.match(line))
    return " ".join(" ".join(kept).split())


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host.removeprefix("www.")


def _optional(value: object) -> str | None:
    return None if value is None or value == "" else str(value)
