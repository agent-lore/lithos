"""Tests for the IndexableDocument seam type and the KM translation helper (#225)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lithos.config import LithosConfig
from lithos.knowledge import KnowledgeDocument, KnowledgeManager, KnowledgeMetadata
from lithos.search import IndexableDocument, SearchEngine


def _make_doc(*, source_url=None, updated_at=None, expires_at=None) -> KnowledgeDocument:
    """Build a KnowledgeDocument with controllable optional fields."""
    return KnowledgeDocument(
        id="11111111-1111-1111-1111-111111111111",
        title="Seam Test Doc",
        content="Body of the doc.",
        path=Path("notes/seam-test.md"),
        metadata=KnowledgeMetadata(
            id="11111111-1111-1111-1111-111111111111",
            title="Seam Test Doc",
            author="alice",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=updated_at,
            tags=["alpha", "beta"],
            source_url=source_url,
            expires_at=expires_at,
        ),
    )


def test_indexable_document_is_frozen() -> None:
    """IndexableDocument is immutable — assigning a field raises FrozenInstanceError."""
    from dataclasses import FrozenInstanceError

    doc = IndexableDocument(
        id="x",
        title="t",
        content="c",
        path="p",
        author="a",
        tags=("one",),
        source_url="",
        updated_at="",
        expires_at="",
    )
    with pytest.raises(FrozenInstanceError):
        doc.title = "mutated"  # type: ignore[misc]


def test_indexable_document_full_content_includes_title_heading() -> None:
    """full_content prepends the title as an H1 — the form Tantivy stores."""
    doc = IndexableDocument(
        id="x",
        title="Hello",
        content="World",
        path="p",
        author="a",
        tags=(),
        source_url="",
        updated_at="",
        expires_at="",
    )
    assert doc.full_content == "# Hello\n\nWorld"


def test_to_indexable_preserves_required_fields() -> None:
    """to_indexable copies id/title/content/author/tags verbatim."""
    doc = _make_doc()
    indexable = KnowledgeManager.to_indexable(doc)
    assert indexable.id == doc.id
    assert indexable.title == doc.title
    assert indexable.content == doc.content
    assert indexable.author == doc.metadata.author
    assert indexable.tags == ("alpha", "beta")


def test_to_indexable_coerces_path_to_string() -> None:
    """Path objects are stringified at the seam — the backends only see str."""
    doc = _make_doc()
    indexable = KnowledgeManager.to_indexable(doc)
    assert indexable.path == str(doc.path)
    assert isinstance(indexable.path, str)


def test_to_indexable_coerces_none_optionals_to_empty_string() -> None:
    """source_url, updated_at, expires_at all coerce None -> ''."""
    doc = _make_doc(source_url=None, updated_at=None, expires_at=None)
    indexable = KnowledgeManager.to_indexable(doc)
    assert indexable.source_url == ""
    assert indexable.updated_at == ""
    assert indexable.expires_at == ""


def test_to_indexable_serialises_datetimes_to_iso() -> None:
    """updated_at and expires_at are written as ISO strings, not datetimes."""
    when = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    expires = datetime(2026, 6, 1, tzinfo=timezone.utc)
    doc = _make_doc(updated_at=when, expires_at=expires)
    indexable = KnowledgeManager.to_indexable(doc)
    assert indexable.updated_at == when.isoformat()
    assert indexable.expires_at == expires.isoformat()


@pytest.mark.asyncio
async def test_search_index_round_trip_through_indexable(
    test_config: LithosConfig,
) -> None:
    """End-to-end: a doc translated through to_indexable is searchable."""
    engine = await SearchEngine.create(test_config)
    doc = _make_doc()
    indexable = KnowledgeManager.to_indexable(doc)

    engine.index(indexable)

    results = engine.full_text_search("Body")
    assert any(r.id == doc.id for r in results)


@pytest.mark.asyncio
async def test_search_remove_by_id(test_config: LithosConfig) -> None:
    """remove(id) deletes from both backends."""
    engine = await SearchEngine.create(test_config)
    indexable = KnowledgeManager.to_indexable(_make_doc())
    engine.index(indexable)

    engine.remove(indexable.id)

    results = engine.full_text_search("Body")
    assert not any(r.id == indexable.id for r in results)
