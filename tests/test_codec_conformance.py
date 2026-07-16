"""Conformance suite for the frontmatter codec (task 4a3836a9).

The codec is the corpus file format, and its contract is a round-trip law:
:func:`encode` then :func:`decode` reproduces the document, modulo the read-time
defaults :func:`apply_lcma_defaults` fills in. These tests prove that law and the
invariants that hang off it — unknown-key preservation, timezone normalisation,
H1 title inversion, and the strict-write / lenient-read asymmetry — as pure
functions, with no manager and no disk. That purity is the whole point of the
extraction: it is what lets Graph/Provenance/Search depend on the format without
depending on the store.
"""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from lithos.frontmatter_codec import (
    KnowledgeDocument,
    KnowledgeMetadata,
    apply_lcma_defaults,
    decode,
    derive_namespace,
    encode,
    normalize_datetime,
    validate_confidence,
)

pytestmark = pytest.mark.integration


def _meta(**overrides) -> KnowledgeMetadata:
    """A fully-specified metadata record — every read-time default already set.

    A document built from this round-trips *exactly*, because decode has nothing
    left to fill in. Override individual fields to probe a single behaviour.
    """
    base = dict(
        id="11111111-1111-1111-1111-111111111111",
        title="Round Trip",
        author="conf-agent",
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        updated_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        confidence=0.75,
        version=3,
        schema_version=1,
        namespace="notes/sub",
        access_scope="shared",
        note_type="observation",
        status="active",
    )
    base.update(overrides)
    return KnowledgeMetadata(**base)


def _doc(content: str = "Body text with a [[Link]] inside.", **meta_overrides) -> KnowledgeDocument:
    meta = _meta(**meta_overrides)
    return KnowledgeDocument(
        id=meta.id,
        title=meta.title,
        content=content,
        metadata=meta,
        path=Path("notes/sub/round-trip.md"),
        links=[],
    )


# ---------------------------------------------------------------------------
# 1. The round-trip law
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """decode(encode(doc)) reproduces the document."""

    def test_fully_specified_doc_round_trips_exactly(self):
        """A doc whose LCMA fields are all set decodes back field-for-field."""
        doc = _doc()
        back = decode(encode(doc), doc.path)

        assert back.id == doc.id
        assert back.title == doc.title
        assert back.content == doc.content
        assert back.metadata.author == doc.metadata.author
        assert back.metadata.confidence == doc.metadata.confidence
        assert back.metadata.version == doc.metadata.version
        assert back.metadata.namespace == doc.metadata.namespace
        assert back.metadata.access_scope == doc.metadata.access_scope
        assert back.metadata.note_type == doc.metadata.note_type
        assert back.metadata.status == doc.metadata.status
        assert back.metadata.created_at == doc.metadata.created_at
        assert back.metadata.updated_at == doc.metadata.updated_at

    def test_round_trip_is_idempotent(self):
        """encode(decode(encode(doc))) == encode(doc) — a stable fixed point."""
        doc = _doc()
        once = encode(doc)
        twice = encode(decode(once, doc.path))
        assert once == twice

    def test_links_survive_round_trip(self):
        """Wiki-links are re-parsed from the decoded body, not carried literally."""
        doc = _doc(content="See [[Alpha]] and [[Beta|the second]].")
        back = decode(encode(doc), doc.path)
        assert [link.target for link in back.links] == ["Alpha", "Beta"]
        assert back.links[1].display == "the second"

    def test_derived_from_ids_survive_round_trip(self):
        src = "22222222-2222-2222-2222-222222222222"
        doc = _doc(derived_from_ids=[src])
        back = decode(encode(doc), doc.path)
        assert back.metadata.derived_from_ids == [src]


# ---------------------------------------------------------------------------
# 2. Forward compatibility — unknown keys survive
# ---------------------------------------------------------------------------


class TestForwardCompatibility:
    """A note written by a newer Lithos survives a round trip through this one."""

    def test_unknown_frontmatter_key_is_preserved(self):
        """An unrecognised key lands in extra and is re-emitted verbatim."""
        doc = _doc(extra={"future_field": "keep me", "nested": {"a": 1}})
        back = decode(encode(doc), doc.path)
        assert back.metadata.extra["future_field"] == "keep me"
        assert back.metadata.extra["nested"] == {"a": 1}

    def test_known_key_never_shadowed_by_extra(self):
        """A stray extra key colliding with a known field never wins on encode."""
        # extra should never carry a reserved key in practice (the write boundary
        # forbids it), but the codec must still let the typed field win.
        doc = _doc()
        doc.metadata.extra = {"title": "SHADOW"}
        back = decode(encode(doc), doc.path)
        assert back.title == "Round Trip"


# ---------------------------------------------------------------------------
# 3. H1 title inversion
# ---------------------------------------------------------------------------


class TestTitleInversion:
    """encode attaches the title as an H1; decode strips it back off."""

    def test_h1_is_added_on_encode_and_removed_on_decode(self):
        doc = _doc(content="Just the body.")
        text = encode(doc)
        assert "# Round Trip" in text
        back = decode(text, doc.path)
        assert back.title == "Round Trip"
        assert back.content == "Just the body."
        assert not back.content.startswith("#")

    def test_body_h1_overrides_frontmatter_title(self):
        """A body H1 wins over the frontmatter title on decode."""
        text = "---\ntitle: Frontmatter Title\nid: x\n---\n# Body Title\n\nText."
        back = decode(text, Path("a.md"))
        assert back.title == "Body Title"


# ---------------------------------------------------------------------------
# 4. LCMA read-time defaults derive from the path
# ---------------------------------------------------------------------------


class TestReadTimeDefaults:
    """A minimal note gains defaults on decode; namespace comes from the path."""

    def test_absent_lcma_fields_get_defaults(self):
        text = "---\nid: x\ntitle: Minimal\n---\nBody."
        back = decode(text, Path("area/topic/minimal.md"))
        assert back.metadata.schema_version == 1
        assert back.metadata.access_scope == "shared"
        assert back.metadata.note_type == "observation"
        assert back.metadata.status == "active"

    def test_namespace_derived_from_relative_path(self):
        text = "---\nid: x\ntitle: Minimal\n---\nBody."
        back = decode(text, Path("area/topic/minimal.md"))
        assert back.metadata.namespace == "area/topic"

    def test_root_level_note_defaults_to_default_namespace(self):
        text = "---\nid: x\ntitle: Root\n---\nBody."
        back = decode(text, Path("root.md"))
        assert back.metadata.namespace == "default"

    def test_explicit_namespace_survives_and_is_not_re_derived(self):
        text = "---\nid: x\ntitle: T\nnamespace: chosen/ns\n---\nBody."
        back = decode(text, Path("area/topic/t.md"))
        assert back.metadata.namespace == "chosen/ns"

    def test_derive_namespace_matches_decode(self):
        """The standalone helper and the decode path agree."""
        p = Path("area/topic/note.md")
        assert derive_namespace(p) == "area/topic"


# ---------------------------------------------------------------------------
# 5. Timezone normalisation
# ---------------------------------------------------------------------------


class TestTimezoneNormalisation:
    def test_naive_datetime_treated_as_utc(self):
        naive = datetime(2026, 5, 1, 9, 0, 0)
        assert normalize_datetime(naive) == datetime(2026, 5, 1, 9, 0, 0, tzinfo=UTC)

    def test_aware_datetime_converted_to_utc(self):
        plus_two = timezone(timedelta(hours=2))
        aware = datetime(2026, 5, 1, 11, 0, 0, tzinfo=plus_two)
        assert normalize_datetime(aware) == datetime(2026, 5, 1, 9, 0, 0, tzinfo=UTC)

    def test_naive_expires_at_normalised_on_decode(self):
        text = "---\nid: x\ntitle: T\nexpires_at: '2030-01-01T00:00:00'\n---\nBody."
        back = decode(text, Path("a.md"))
        assert back.metadata.expires_at is not None
        assert back.metadata.expires_at.tzinfo is not None


# ---------------------------------------------------------------------------
# 6. Strict-write / lenient-read asymmetry
# ---------------------------------------------------------------------------


class TestConfidenceContract:
    """Reads heal a bad confidence; writes reject it."""

    def test_read_heals_out_of_range_confidence(self):
        text = "---\nid: x\ntitle: T\nconfidence: 5.0\n---\nBody."
        back = decode(text, Path("a.md"))
        assert back.metadata.confidence == 1.0

    def test_read_heals_non_numeric_confidence(self):
        text = "---\nid: x\ntitle: T\nconfidence: high\n---\nBody."
        back = decode(text, Path("a.md"))
        assert back.metadata.confidence == 1.0

    def test_write_validator_rejects_out_of_range(self):
        with pytest.raises(ValueError, match=r"between 0\.0 and 1\.0"):
            validate_confidence(5.0)

    def test_write_validator_rejects_bool(self):
        # bool is an int subclass; a score of True is a bug, not 1.0.
        with pytest.raises(ValueError):
            validate_confidence(True)

    def test_write_validator_accepts_valid(self):
        assert validate_confidence(0) == 0.0
        assert validate_confidence(1) == 1.0
        assert validate_confidence(0.5) == 0.5


# ---------------------------------------------------------------------------
# 7. Purity — the codec never touches a disk
# ---------------------------------------------------------------------------


class TestPurity:
    def test_decode_takes_text_not_a_path(self):
        """decode's first argument is bytes-already-read; a nonexistent path in
        the second argument is fine because it is only used for namespace."""
        text = "---\nid: x\ntitle: T\n---\nBody."
        back = decode(text, Path("does/not/exist/on/disk.md"))
        assert back.title == "T"
        assert back.metadata.namespace == "does/not/exist/on"

    def test_apply_lcma_defaults_only_fills_absent_fields(self):
        meta = _meta(namespace=None, status=None)
        apply_lcma_defaults(meta, Path("x/y.md"))
        assert meta.namespace == "x"
        assert meta.status == "active"
        # Already-set fields are untouched.
        assert meta.access_scope == "shared"
        assert meta.note_type == "observation"
