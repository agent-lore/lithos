"""What the typed-edge adjudicator sees of one note (prompt v2).

Prompt v1 showed the focus note's first characters and, for each candidate,
the one search chunk that best matched the focus. On the 2026-11 golden set
that chunk was often Influx's relevance blurb rather than the note's content,
and only one candidate in ten showed a date or source, so the model could not
tell which of two near-duplicates came first.

v2 shows every note the same way: a line with the date it was added and its
source, then the head of its summary, then (for a candidate) whatever the
matching chunk adds, if there is room. Influx's archive path and its
profile-relevance opinions are never shown, from the body or the chunk. The
functions here are pure, so the engine, the eval runner and the labelling
sheet render identical text from the same :class:`NoteView`.
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

#: Sections never shown, from a summary, a plain body or a matching chunk:
#: Influx's archive path and its views of the note's fit to a profile.
EXCLUDED_SECTIONS = frozenset({"archive", "relevance", "profile relevance"})
#: The first line of a per-profile section (``### robotics`` under ``## Profile
#: Relevance``); such a section is excluded even when a chunk lacks its parent.
_PROFILE_SCORE = re.compile(r"^Score: *\d+(\.\d+)? */ *10\b")

#: A matching chunk is only worth adding after other text when this much room
#: is left.
_MIN_CHUNK_ROOM = 120

_H2 = re.compile(r"^## +(.+?)[ \t]*$", re.MULTILINE)
_H3 = re.compile(r"^### +(.+?)[ \t]*$", re.MULTILINE)
_HEADING = re.compile(r"^(#{1,6}) +(.+?)[ \t]*$")
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
    (the search chunk that matched the focus). The chunk loses the same
    excluded sections and title lines as the body, every line the text already
    shows, and any heading left with nothing under it; what remains is shown
    only if it has prose. After other text it needs at least
    :data:`_MIN_CHUNK_ROOM` characters of room; when it is the only text (a
    note that could not be read), it is shown within *limit* regardless.
    """
    core, optional = _body(view)
    text = _join(provenance_line(view), core)
    if optional and len(text) + 1 + len(optional) <= limit:
        text = _join(text, optional)
    passage = _new_material(_without_excluded(chunk, view.title), text) if chunk else ""
    if passage:
        if not text:
            text = f"Matching passage: {passage}"
        elif limit - len(text) - 1 >= _MIN_CHUNK_ROOM:
            text = _join(text, f"Matching passage: {passage}")
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
    return _without_excluded(view.content, view.title), ""


def _subsection(summary_text: str, name: str) -> str:
    span = _section(summary_text, _H3, name)
    body = summary_text[span[1] : span[2]].strip() if span is not None else ""
    return f"{name}:\n{body}" if body else ""


def _join(*parts: str) -> str:
    return "\n".join(p for p in parts if p)


def _without_excluded(text: str, title: str) -> str:
    """*text* without its title lines and the :data:`EXCLUDED_SECTIONS`.

    A section runs from its heading to the next heading of the same or a higher
    level, so ``## Profile Relevance`` takes its per-profile ``###`` parts with
    it. The search index splits notes at blank lines, and a heading has no blank
    line before its first paragraph, so a chunk always holds a section's
    heading with its first lines. A chunk can start at a later per-profile
    section without its ``## Profile Relevance`` parent, so a section whose
    first line is a ``Score: N/10`` is excluded too. The title appears as
    ``# Title`` in a body and also bare at the start of a note's first chunk.
    """
    lines = text.splitlines()
    kept: list[str] = []
    skipping: int | None = None  # heading level of the excluded section being skipped
    for i, line in enumerate(lines):
        heading = _HEADING.match(line.strip())
        if heading:
            level, name = len(heading.group(1)), heading.group(2).strip()
            if skipping is not None and level <= skipping:
                skipping = None
            following = next((nxt.strip() for nxt in lines[i + 1 :] if nxt.strip()), "")
            if skipping is None and (
                name.lower() in EXCLUDED_SECTIONS or _PROFILE_SCORE.match(following)
            ):
                skipping = level
            if skipping is not None or (level == 1 and name == title.strip()):
                continue
        if skipping is not None or line.strip() == title.strip():
            continue
        kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


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


def _new_material(passage: str, text: str) -> str:
    """*passage* without the lines *text* already shows; ``""`` when no prose is left."""
    shown = _prose(text)
    lines = [
        line
        for line in passage.splitlines()
        if _HEADING_LINE.match(line.strip()) or not _prose(line) or _prose(line) not in shown
    ]
    rest = _without_empty_headings(lines)
    return rest if _prose(rest) else ""


def _without_empty_headings(lines: list[str]) -> str:
    """The lines without headings that have no prose before the next heading at their level."""
    kept: list[str] = []
    for i, line in enumerate(lines):
        heading = _HEADING.match(line.strip())
        if heading:
            level = len(heading.group(1))
            body = False
            for following in lines[i + 1 :]:
                sub = _HEADING.match(following.strip())
                if sub and len(sub.group(1)) <= level:
                    break
                if not sub and following.strip():
                    body = True
                    break
            if not body:
                continue
        kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


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
