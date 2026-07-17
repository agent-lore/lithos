"""Direct unit tests for :class:`lithos.corpus_index.CorpusIndex`.

CorpusIndex is a pure in-memory projection — no disk, no async — so these tests
drive its public surface directly, without a KnowledgeManager. They pin the
behaviour KnowledgeManager relies on: document lifecycle, provenance
classification/auto-resolve, source-URL collision counting, whole-corpus
rebuild, and the ``provenance_neighbours`` BFS relocated here in PR4b.
"""

from __future__ import annotations

from pathlib import Path

from lithos.corpus_index import CachedMeta, CorpusIndex, ScannedNote

MISSING_SOURCE_WARNING = "derived_from_ids contains missing document: {}"


def _meta(idx: CorpusIndex, *, title: str = "", path: str = "n.md", **fm: object) -> CachedMeta:
    """Build a CachedMeta the cheap way, from a frontmatter dict."""
    fm.setdefault("title", title)
    return CachedMeta.from_frontmatter(dict(fm), Path(path), seq=idx.next_seq())


def _add(
    idx: CorpusIndex,
    doc_id: str,
    *,
    title: str,
    sources: list[str] | None = None,
    path: str | None = None,
    norm_url: str | None = None,
    **fm: object,
) -> list[str]:
    """Register a document and return the create-path warnings."""
    rel = path or f"{doc_id}.md"
    cached = _meta(idx, title=title, path=rel, **fm)
    return idx.add_document(doc_id, cached, title=title, norm_url=norm_url, sources=sources or [])


# ---------------------------------------------------------------------------
# provenance_neighbours — the BFS relocated from tools/read_search.py (PR4b)
# ---------------------------------------------------------------------------


def _chain_a_b_c() -> CorpusIndex:
    """A derives from B, B derives from C (added in dependency order)."""
    idx = CorpusIndex()
    _add(idx, "C", title="Cee")
    _add(idx, "B", title="Bee", sources=["C"])
    _add(idx, "A", title="Aay", sources=["B"])
    return idx


def test_provenance_neighbours_sources_single_hop():
    # Arrange
    idx = _chain_a_b_c()

    # Act
    result = idx.provenance_neighbours("A", "sources", 1)

    # Assert
    assert result == [{"id": "B", "title": "Bee"}]


def test_provenance_neighbours_sources_multi_hop_is_transitive():
    # Arrange
    idx = _chain_a_b_c()

    # Act
    depth2 = idx.provenance_neighbours("A", "sources", 2)
    depth3 = idx.provenance_neighbours("A", "sources", 3)

    # Assert — depth 2 reaches C through B; depth 3 finds nothing further
    assert depth2 == [{"id": "B", "title": "Bee"}, {"id": "C", "title": "Cee"}]
    assert depth3 == depth2


def test_provenance_neighbours_derived_direction_walks_reverse_edges():
    # Arrange
    idx = _chain_a_b_c()

    # Act
    one_hop = idx.provenance_neighbours("C", "derived", 1)
    two_hop = idx.provenance_neighbours("C", "derived", 2)

    # Assert
    assert one_hop == [{"id": "B", "title": "Bee"}]
    assert two_hop == [{"id": "A", "title": "Aay"}, {"id": "B", "title": "Bee"}]


def test_provenance_neighbours_excludes_start_and_sorts_by_id():
    # Arrange — one doc derived from two sources
    idx = CorpusIndex()
    _add(idx, "src-z", title="Zed")
    _add(idx, "src-a", title="Ann")
    _add(idx, "doc", title="Doc", sources=["src-z", "src-a"])

    # Act
    result = idx.provenance_neighbours("doc", "sources", 1)

    # Assert — start excluded, output sorted by id
    ids = [n["id"] for n in result]
    assert ids == ["src-a", "src-z"]
    assert "doc" not in ids


def test_provenance_neighbours_sources_skips_dangling_reference():
    # Arrange — a note pointing at a source that was never created
    idx = CorpusIndex()
    _add(idx, "A", title="Aay", sources=["ghost"])

    # Act
    result = idx.provenance_neighbours("A", "sources", 2)

    # Assert — the has_document guard drops the dangling id
    assert result == []
    assert idx.unresolved_sources("A") == ["ghost"]


def test_provenance_neighbours_empty_when_no_provenance():
    # Arrange
    idx = CorpusIndex()
    _add(idx, "solo", title="Solo")

    # Act / Assert
    assert idx.provenance_neighbours("solo", "sources", 3) == []
    assert idx.provenance_neighbours("solo", "derived", 3) == []


def test_provenance_neighbours_dedupes_diamond():
    # Arrange — diamond: A derives from B and C; both derive from D
    idx = CorpusIndex()
    _add(idx, "D", title="Dee")
    _add(idx, "B", title="Bee", sources=["D"])
    _add(idx, "C", title="Cee", sources=["D"])
    _add(idx, "A", title="Aay", sources=["B", "C"])

    # Act — D reachable via both B and C, must appear once
    result = idx.provenance_neighbours("A", "sources", 3)

    # Assert
    ids = [n["id"] for n in result]
    assert ids == ["B", "C", "D"]


# ---------------------------------------------------------------------------
# Document lifecycle
# ---------------------------------------------------------------------------


def test_add_document_registers_across_indexes():
    # Arrange
    idx = CorpusIndex()

    # Act
    warnings = _add(idx, "A", title="Alpha", path="alpha.md")

    # Assert
    assert warnings == []
    assert idx.has_document("A")
    assert idx.relpath_of("A") == Path("alpha.md")
    assert idx.id_by_slug("alpha") == "A"
    assert idx.id_by_relpath(Path("alpha.md")) == "A"
    assert idx.title_by_id("A") == "Alpha"
    assert idx.get_cached_meta("A") is not None


def test_add_document_warns_on_dangling_source():
    # Arrange / Act
    idx = CorpusIndex()
    warnings = _add(idx, "A", title="Alpha", sources=["nope"])

    # Assert
    assert warnings == [MISSING_SOURCE_WARNING.format("nope")]
    assert idx.has_unresolved_source("nope")


def test_add_document_auto_resolves_prior_danglers():
    # Arrange — B references A before A exists
    idx = CorpusIndex()
    _add(idx, "B", title="Bee", sources=["A"])
    assert idx.has_unresolved_source("A")

    # Act — creating A promotes B's reference to resolved
    _add(idx, "A", title="Aay")

    # Assert
    assert not idx.has_unresolved_source("A")
    assert idx.derived_docs("A") == {"B"}


def test_remove_document_clears_indexes_and_reorphans_derived():
    # Arrange — B derives from A
    idx = CorpusIndex()
    _add(idx, "A", title="Aay")
    _add(idx, "B", title="Bee", sources=["A"])
    assert idx.derived_docs("A") == {"B"}

    # Act — removing the source A re-orphans B's reference
    idx.remove_document("A")

    # Assert
    assert not idx.has_document("A")
    assert idx.id_by_slug("aay") is None
    assert idx.get_cached_meta("A") is None
    assert idx.derived_docs("A") == set()
    assert idx.has_unresolved_source("A")


def test_replace_provenance_swaps_sources_and_reports_missing():
    # Arrange — A derives from B
    idx = CorpusIndex()
    _add(idx, "B", title="Bee")
    _add(idx, "C", title="Cee")
    _add(idx, "A", title="Aay", sources=["B"])
    assert idx.derived_docs("B") == {"A"}

    # Act — repoint A at C plus a missing id
    warnings = idx.replace_provenance("A", ["C", "gone"])

    # Assert
    assert idx.doc_sources("A") == ["C", "gone"]
    assert idx.derived_docs("B") == set()  # old reverse edge dropped
    assert idx.derived_docs("C") == {"A"}
    assert warnings == [MISSING_SOURCE_WARNING.format("gone")]


# ---------------------------------------------------------------------------
# rebuild — whole-corpus scan
# ---------------------------------------------------------------------------


def _note(doc_id: str, title: str, **fm: object) -> ScannedNote:
    return ScannedNote(
        doc_id=doc_id,
        title=title,
        frontmatter={"title": title, **fm},
        rel_path=Path(f"{doc_id}.md"),
    )


def test_rebuild_populates_indexes_and_provenance_from_scan():
    # Arrange — two notes, one derived from the other. rebuild() normalizes
    # derived_from_ids through the lenient UUID validator (unlike add_document),
    # so the ids must be real UUIDs to survive the scan.
    a_id = "11111111-1111-4111-8111-111111111111"
    b_id = "22222222-2222-4222-8222-222222222222"
    idx = CorpusIndex()
    scanned = [
        _note(a_id, "Aay"),
        _note(b_id, "Bee", derived_from_ids=[a_id]),
    ]

    # Act
    idx.rebuild(scanned)

    # Assert — core maps + cross-pass provenance wiring
    assert idx.has_document(a_id) and idx.has_document(b_id)
    assert idx.id_by_slug("bee") == b_id
    assert idx.get_cached_meta(b_id) is not None
    assert idx.doc_sources(b_id) == [a_id]
    assert idx.derived_docs(a_id) == {b_id}
    assert idx.provenance_neighbours(b_id, "sources", 1) == [{"id": a_id, "title": "Aay"}]


def test_rebuild_clears_prior_state():
    # Arrange
    idx = CorpusIndex()
    idx.rebuild([_note("A", "Aay"), _note("B", "Bee")])

    # Act — a second scan with a smaller corpus
    idx.rebuild([_note("A", "Aay")])

    # Assert — the dropped note is gone, no accumulation
    assert idx.has_document("A")
    assert not idx.has_document("B")
    assert idx.id_by_slug("bee") is None


def test_rebuild_counts_source_url_collisions_first_writer_wins():
    # Arrange — two notes claiming the same source_url
    url = "https://example.com/page"
    idx = CorpusIndex()

    # Act
    idx.rebuild(
        [
            _note("first", "First", source_url=url),
            _note("second", "Second", source_url=url),
        ]
    )

    # Assert — one duplicate counted, first note keeps ownership
    assert idx.duplicate_url_count == 1
    from lithos.frontmatter_codec import normalize_url

    assert idx.source_url_owner(normalize_url(url)) == "first"
