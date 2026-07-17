"""The in-memory projection over the Corpus: KnowledgeManager's query accelerator.

The Corpus is the source of truth; this is the derived view that makes it
queryable without a disk read. It owns the per-document metadata cache, the five
inverted indexes that turn equality filters into set intersections (#306/#316),
the path/slug/source-url maps, and the in-memory provenance graph
(``derived_from`` forward + reverse). All of it is rebuilt from the Corpus on
construction and maintained incrementally on every write.

It lives apart from :mod:`lithos.knowledge` because it is a *view*, not the
store. :class:`KnowledgeManager` owns files, slug allocation, dedup policy and
validation; it holds one :class:`CorpusIndex` and routes the derived-state
mutations through it. Naming the projection keeps query-acceleration bugs in one
module and lets :class:`CachedMeta` cross the seam as a real value type (as
:class:`~lithos.search.IndexableDocument` does for Search) instead of being
re-projected by every consumer.

Unlike Tantivy, the link graph, and ``edges.db``, this view never persists: it is
rebuilt from :meth:`KnowledgeManager.scan_corpus` every process start, so it
cannot drift across processes and needs no reconcile — it is always consistent
with the Corpus its manager last scanned. Pure in-memory, no disk access: the
manager reads files and hands over frontmatter.
"""

from __future__ import annotations

import collections
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from lithos.frontmatter_codec import (
    KnowledgeMetadata,
    canonical_metadata_value,
    derive_namespace,
    extract_extra,
    normalize_datetime,
    normalize_derived_from_ids_lenient,
    normalize_url,
    slugify,
)

logger = logging.getLogger(__name__)


@dataclass
class CachedMeta:
    """Lightweight metadata cache for filtering without disk I/O.

    ``namespace`` is the resolved LCMA namespace — either an explicit value
    from frontmatter or the path-derived default. Callers MUST read this
    field rather than re-deriving from ``path`` so explicit overrides are
    honored consistently across the retrieval pipeline.
    """

    title: str
    author: str
    tags: list[str]
    updated_at: datetime
    path: Path
    namespace: str
    expires_at: datetime | None = None
    access_scope: str | None = None
    source: str | None = None
    note_type: str | None = None
    status: str | None = None
    source_url: str | None = None
    # Extracted/curated entity names (#316) — kept here so the entities
    # inverted index can be maintained without a disk read.
    entities: list[str] = field(default_factory=list)
    # Free-form key/value metadata (#305) — kept here so equality filtering and
    # the inverted index (#306) work without a disk read.
    extra: dict = field(default_factory=dict)
    # Insertion ordinal — reproduces _meta_cache ordering in the index path so
    # list pagination stays stable (#306).
    seq: int = 0

    @classmethod
    def from_metadata(cls, metadata: KnowledgeMetadata, path: Path, *, seq: int) -> CachedMeta:
        """Build a cache entry from a document's typed metadata.

        The namespace is the explicit frontmatter value when set, otherwise the
        path-derived default — matching ``apply_lcma_defaults`` at read time.
        Shared by create/update/sync so the field mapping lives in one place.
        """
        return cls(
            title=metadata.title,
            author=metadata.author,
            tags=list(metadata.tags),
            updated_at=metadata.updated_at,
            path=path,
            namespace=metadata.namespace or derive_namespace(path),
            expires_at=metadata.expires_at,
            access_scope=metadata.access_scope,
            source=metadata.source,
            note_type=metadata.note_type,
            status=metadata.status,
            source_url=metadata.source_url,
            entities=list(metadata.entities),
            extra=dict(metadata.extra),
            seq=seq,
        )

    @classmethod
    def from_frontmatter(cls, meta: dict, rel_path: Path, *, seq: int) -> CachedMeta:
        """Build a cache entry from raw frontmatter during a startup scan.

        Defensive by construction: frontmatter is hand-editable, so every field
        is type-guarded and falls back to a safe default rather than trusting
        the on-disk value. This is the cheap path — no full document decode.
        """
        raw_updated = meta.get("updated_at")
        if isinstance(raw_updated, str):
            updated_at = datetime.fromisoformat(raw_updated)
        elif isinstance(raw_updated, datetime):
            updated_at = raw_updated
        else:
            updated_at = datetime.now(UTC)

        raw_expires = meta.get("expires_at")
        if isinstance(raw_expires, str):
            try:
                cached_expires: datetime | None = datetime.fromisoformat(raw_expires)
            except ValueError:
                cached_expires = None
        elif isinstance(raw_expires, datetime):
            cached_expires = raw_expires
        else:
            cached_expires = None

        raw_namespace = meta.get("namespace")
        namespace = (
            raw_namespace
            if isinstance(raw_namespace, str) and raw_namespace
            else derive_namespace(rel_path)
        )
        raw_tags = meta.get("tags", [])
        raw_author = meta.get("author", "")
        raw_access_scope = meta.get("access_scope")
        raw_source = meta.get("source")
        raw_note_type = meta.get("note_type")
        raw_status = meta.get("status")
        raw_source_url = meta.get("source_url")
        raw_entities = meta.get("entities", [])
        return cls(
            title=meta.get("title", "") if isinstance(meta.get("title"), str) else "",
            author=raw_author if isinstance(raw_author, str) else "",
            tags=raw_tags if isinstance(raw_tags, list) else [],
            updated_at=updated_at,
            path=rel_path,
            namespace=namespace,
            expires_at=cached_expires,
            access_scope=raw_access_scope if isinstance(raw_access_scope, str) else None,
            source=raw_source if isinstance(raw_source, str) else None,
            note_type=raw_note_type if isinstance(raw_note_type, str) else None,
            status=raw_status if isinstance(raw_status, str) else None,
            source_url=raw_source_url if isinstance(raw_source_url, str) else None,
            entities=raw_entities if isinstance(raw_entities, list) else [],
            extra=extract_extra(meta),
            seq=seq,
        )


@dataclass(frozen=True)
class ScannedNote:
    """One note as seen by a startup scan — the input row to :meth:`CorpusIndex.rebuild`.

    Carries the frontmatter the manager already read plus the resolved id/title,
    so the index does the projection while the manager keeps the file I/O.
    """

    doc_id: str
    title: str
    frontmatter: dict
    rel_path: Path


class CorpusIndex:
    """The derived, in-memory view of the Corpus that KnowledgeManager queries.

    Holds all derived state privately and exposes an explicit read + mutation
    surface. The manager never touches the maps directly; it calls the methods
    here, which keeps every query-acceleration invariant in one place.
    """

    def __init__(self) -> None:
        self._id_to_path: dict[str, Path] = {}
        self._path_to_id: dict[Path, str] = {}
        self._slug_to_id: dict[str, str] = {}
        self._source_url_to_id: dict[str, str] = {}
        # Provenance indexes
        self._doc_to_sources: dict[str, list[str]] = {}
        self._source_to_derived: dict[str, set[str]] = {}
        self._unresolved_provenance: dict[str, set[str]] = {}
        self._id_to_title: dict[str, str] = {}
        self._meta_cache: dict[str, CachedMeta] = {}
        # Inverted indexes for sub-linear equality filtering (#306). All map a
        # value to the set of doc ids carrying it; maintained beside _meta_cache.
        self._author_index: dict[str, set[str]] = {}
        self._status_index: dict[str, set[str]] = {}
        self._tag_index: dict[str, set[str]] = {}
        self._entities_index: dict[str, set[str]] = {}
        self._metadata_index: dict[str, dict[str, set[str]]] = {}
        self._meta_seq: int = 0
        self.duplicate_url_count: int = 0

    def next_seq(self) -> int:
        """Return a monotonically increasing insertion ordinal for CachedMeta.seq."""
        self._meta_seq += 1
        return self._meta_seq

    # ==================== Inverted-index maintenance ====================

    def index_doc(self, doc_id: str, cached: CachedMeta) -> None:
        """Add a doc's equality-filter contributions to the inverted indexes."""
        if cached.author:
            self._author_index.setdefault(cached.author, set()).add(doc_id)
        if cached.status:
            self._status_index.setdefault(cached.status, set()).add(doc_id)
        for tag in cached.tags:
            self._tag_index.setdefault(tag, set()).add(doc_id)
        for entity in cached.entities:
            self._entities_index.setdefault(entity, set()).add(doc_id)
        for key, value in cached.extra.items():
            buckets = self._metadata_index.setdefault(key, {})
            # A list value is matched element-wise (contains); a scalar by value.
            elements = value if isinstance(value, list) else [value]
            for element in elements:
                buckets.setdefault(canonical_metadata_value(element), set()).add(doc_id)

    def deindex_doc(self, doc_id: str, cached: CachedMeta) -> None:
        """Remove a doc's contributions (using its *previous* cached meta)."""

        def _discard(index: dict[str, set[str]], value: str | None) -> None:
            if value is None:
                return
            bucket = index.get(value)
            if bucket is not None:
                bucket.discard(doc_id)
                if not bucket:
                    del index[value]

        _discard(self._author_index, cached.author or None)
        _discard(self._status_index, cached.status or None)
        for tag in cached.tags:
            _discard(self._tag_index, tag)
        for entity in cached.entities:
            _discard(self._entities_index, entity)
        for key, value in cached.extra.items():
            buckets = self._metadata_index.get(key)
            if buckets is None:
                continue
            elements = value if isinstance(value, list) else [value]
            for element in elements:
                _discard(buckets, canonical_metadata_value(element))
            if not buckets:
                del self._metadata_index[key]

    def candidate_ids(
        self,
        *,
        tags: list[str] | None,
        author: str | None,
        metadata_match: dict | None,
        exclude_status: list[str] | None,
        entities: list[str] | None = None,
    ) -> set[str] | None:
        """Resolve equality filters to a candidate id set via the inverted index.

        Returns ``None`` when no equality/AND filter is supplied, signalling the
        caller to use the existing full-scan path (correct for unfiltered /
        prefix-only / since-only / exclude-only queries). Otherwise returns the
        (possibly empty) intersected candidate set — never iterating all docs.
        """
        seed_sets: list[set[str]] = []
        if author:
            seed_sets.append(self._author_index.get(author, set()))
        if tags:
            for tag in tags:
                seed_sets.append(self._tag_index.get(tag, set()))
        if entities:
            for entity in entities:
                seed_sets.append(self._entities_index.get(entity, set()))
        if metadata_match:
            for key, value in metadata_match.items():
                bucket = self._metadata_index.get(key, {})
                seed_sets.append(bucket.get(canonical_metadata_value(value), set()))

        if not seed_sets:
            return None

        # Intersect smallest-first; any empty seed short-circuits to empty.
        seed_sets.sort(key=len)
        candidates = set(seed_sets[0])
        for s in seed_sets[1:]:
            candidates &= s
            if not candidates:
                break

        if candidates and exclude_status:
            for status in exclude_status:
                candidates -= self._status_index.get(status, set())

        return candidates

    def metadata_candidate_ids(self, metadata_match: dict | None) -> set[str] | None:
        """Candidate ids for a ``metadata_match`` filter, or ``None`` if empty (#306)."""
        if not metadata_match:
            return None
        return self.candidate_ids(
            tags=None, author=None, metadata_match=metadata_match, exclude_status=None
        )

    def entities_candidate_ids(self, entities: list[str] | None) -> set[str] | None:
        """Candidate ids carrying every named entity, or ``None`` if empty (#316)."""
        if not entities:
            return None
        return self.candidate_ids(
            tags=None, author=None, metadata_match=None, exclude_status=None, entities=entities
        )

    # ==================== Provenance maintenance ====================

    def _classify_provenance(self, doc_id: str, sources: list[str]) -> list[str]:
        """Record ``doc_id``'s sources and file each into resolved/unresolved.

        Sets the forward map and, per source, adds ``doc_id`` to
        ``_source_to_derived`` when the source exists or ``_unresolved_provenance``
        when it does not. Returns a warning per missing source. Shared core of
        create/update/sync/scan — the callers own removal, auto-resolve, and
        whether to surface the warnings.
        """
        self._doc_to_sources[doc_id] = list(sources)
        warnings: list[str] = []
        for source_id in sources:
            if source_id in self._id_to_path:
                self._source_to_derived.setdefault(source_id, set()).add(doc_id)
            else:
                self._unresolved_provenance.setdefault(source_id, set()).add(doc_id)
                warnings.append(f"derived_from_ids contains missing document: {source_id}")
        return warnings

    def _classify_and_log(self, doc_id: str, sources: list[str]) -> list[str]:
        """Classify provenance and log each unresolved source. Returns warnings.

        The write paths (create/update) surface warnings to the caller and log
        the dangling reference; the silent paths (sync/scan) call
        :meth:`_classify_provenance` directly.
        """
        warnings = self._classify_provenance(doc_id, sources)
        for source_id in sources:
            if source_id not in self._id_to_path:
                logger.warning(
                    "Provenance resolution failed: source_id=%s dependent_doc_id=%s",
                    source_id,
                    doc_id,
                )
        return warnings

    def _auto_resolve(self, doc_id: str) -> None:
        """Promote docs that referenced ``doc_id`` before it existed to resolved."""
        if doc_id in self._unresolved_provenance:
            resolved_docs = self._unresolved_provenance.pop(doc_id)
            self._source_to_derived.setdefault(doc_id, set()).update(resolved_docs)

    def remove_provenance(self, doc_id: str) -> None:
        """Remove a document's forward references from the reverse indexes.

        Cleans ``_source_to_derived`` and ``_unresolved_provenance`` for the
        given doc_id based on its current ``_doc_to_sources`` entries. Leaves
        the forward ``_doc_to_sources`` entry untouched (callers overwrite it).
        """
        old_sources = self._doc_to_sources.get(doc_id, [])
        for source_id in old_sources:
            if source_id in self._source_to_derived:
                self._source_to_derived[source_id].discard(doc_id)
                if not self._source_to_derived[source_id]:
                    del self._source_to_derived[source_id]
            if source_id in self._unresolved_provenance:
                self._unresolved_provenance[source_id].discard(doc_id)
                if not self._unresolved_provenance[source_id]:
                    del self._unresolved_provenance[source_id]

    def replace_provenance(self, doc_id: str, sources: list[str]) -> list[str]:
        """Replace a document's provenance: drop old reverse entries, classify new.

        The update path's semantics — logs and returns a warning per missing
        source, but does not auto-resolve (a re-pointed note is not a new node).
        """
        self.remove_provenance(doc_id)
        return self._classify_and_log(doc_id, sources)

    def sync_provenance(self, doc_id: str, sources: list[str], *, is_new: bool) -> None:
        """Diff-and-apply provenance during a file-watcher sync.

        A modified note only re-files when its sources changed; a new note
        classifies and then auto-resolves anything that referenced it.
        """
        if not is_new:
            if self._doc_to_sources.get(doc_id, []) != sources:
                self.remove_provenance(doc_id)
                self._classify_provenance(doc_id, sources)
        else:
            self._classify_provenance(doc_id, sources)
            self._auto_resolve(doc_id)

    # ==================== Document mutation ====================

    def add_document(
        self,
        doc_id: str,
        cached: CachedMeta,
        *,
        title: str,
        norm_url: str | None,
        sources: list[str],
    ) -> list[str]:
        """Register a freshly-created document across every index. Returns warnings.

        Warnings name any source in ``sources`` that does not yet exist (the
        note still records the dangling reference). Also auto-resolves docs that
        referenced ``doc_id`` before it existed.
        """
        self._id_to_path[doc_id] = cached.path
        self._path_to_id[cached.path] = doc_id
        self._slug_to_id[slugify(title)] = doc_id
        if norm_url is not None:
            self._source_url_to_id[norm_url] = doc_id
        self._id_to_title[doc_id] = title

        warnings = self._classify_and_log(doc_id, sources)
        self._auto_resolve(doc_id)

        self._meta_cache[doc_id] = cached
        self.index_doc(doc_id, cached)
        return warnings

    def reindex_document(self, doc_id: str, cached: CachedMeta) -> None:
        """Replace a document's cache entry and inverted-index contributions.

        Deindexes the prior entry first (if any); the caller preserves ``seq``
        on ``cached`` so list ordering/pagination is unchanged by an update.
        """
        old_cached = self._meta_cache.get(doc_id)
        if old_cached is not None:
            self.deindex_doc(doc_id, old_cached)
        self._meta_cache[doc_id] = cached
        self.index_doc(doc_id, cached)

    def remove_document(self, doc_id: str) -> None:
        """Remove a document from every index (id/path/slug/title/provenance/cache)."""
        old_path = self._id_to_path.pop(doc_id, None)
        if old_path is not None:
            self._path_to_id.pop(old_path, None)
        self._slug_to_id = {k: v for k, v in self._slug_to_id.items() if v != doc_id}

        # 1. Remove this doc as a "derived" doc from reverse indexes
        self.remove_provenance(doc_id)
        # 2. Remove forward index entry
        self._doc_to_sources.pop(doc_id, None)
        # 3. If this doc was a source for others, move those to unresolved
        derived_docs = self._source_to_derived.pop(doc_id, set())
        if derived_docs:
            self._unresolved_provenance[doc_id] = derived_docs
        # 4. Remove from title and metadata caches + inverted index
        self._id_to_title.pop(doc_id, None)
        removed = self._meta_cache.pop(doc_id, None)
        if removed is not None:
            self.deindex_doc(doc_id, removed)

    def set_title(self, doc_id: str, title: str) -> None:
        """Record a document's title for id→title lookups."""
        self._id_to_title[doc_id] = title

    def set_path(self, doc_id: str, rel_path: Path) -> None:
        """Point ``doc_id`` at ``rel_path``, dropping any prior path mapping.

        Handles a file-watcher rename where a known doc moves to a new path.
        """
        old_path = self._id_to_path.get(doc_id)
        if old_path is not None:
            self._path_to_id.pop(old_path, None)
        self._id_to_path[doc_id] = rel_path
        self._path_to_id[rel_path] = doc_id

    def reroute_slug_for(self, doc_id: str, new_slug: str) -> None:
        """Move ``doc_id`` to ``new_slug``, finding its current slug by scan.

        The sync path does not carry the old slug (the on-disk title may have
        changed under it), so the previous entry is located by value.
        """
        old_slug = next((s for s, sid in self._slug_to_id.items() if sid == doc_id), None)
        if old_slug and old_slug != new_slug and self._slug_to_id.get(old_slug) == doc_id:
            del self._slug_to_id[old_slug]
        self._slug_to_id[new_slug] = doc_id

    def reroute_slug(self, old_slug: str, new_slug: str, doc_id: str) -> None:
        """Move ``doc_id``'s slug entry from ``old_slug`` to ``new_slug``."""
        if old_slug != new_slug:
            if self._slug_to_id.get(old_slug) == doc_id:
                del self._slug_to_id[old_slug]
            self._slug_to_id[new_slug] = doc_id

    def set_source_url(self, norm_url: str, doc_id: str) -> None:
        """Point a normalized source URL at ``doc_id``."""
        self._source_url_to_id[norm_url] = doc_id

    def remove_source_url(self, norm_url: str, doc_id: str) -> None:
        """Drop a normalized source URL mapping if ``doc_id`` currently owns it."""
        if self._source_url_to_id.get(norm_url) == doc_id:
            del self._source_url_to_id[norm_url]

    def clear_source_urls_for(self, doc_id: str) -> None:
        """Drop every source-URL mapping currently pointing at ``doc_id``."""
        for k in [k for k, v in self._source_url_to_id.items() if v == doc_id]:
            del self._source_url_to_id[k]

    def source_url_owner(self, norm_url: str) -> str | None:
        """Return the doc id currently owning ``norm_url``, or ``None``."""
        return self._source_url_to_id.get(norm_url)

    def rebuild(self, scanned: Iterable[ScannedNote]) -> None:
        """Rebuild every index from a startup scan of the Corpus.

        Two passes over the (already file-read) notes: pass 1 builds the core
        maps, the metadata cache, and the source-url map (tracking duplicates);
        pass 2 classifies each note's provenance now that every id is known.
        Clears all prior state first, so a rescan can never accumulate staleness.
        """
        self._id_to_path.clear()
        self._path_to_id.clear()
        self._slug_to_id.clear()
        self._source_url_to_id.clear()
        self._doc_to_sources.clear()
        self._source_to_derived.clear()
        self._unresolved_provenance.clear()
        self._id_to_title.clear()
        self._meta_cache.clear()
        self._author_index.clear()
        self._status_index.clear()
        self._tag_index.clear()
        self._entities_index.clear()
        self._metadata_index.clear()
        self._meta_seq = 0
        self.duplicate_url_count = 0

        collisions: list[tuple[str, str, str]] = []  # (norm_url, first_id, dup_id)
        deferred_provenance: list[tuple[str, list[str]]] = []

        # Pass 1: populate core indexes, collect provenance.
        for note in scanned:
            doc_id = note.doc_id
            self._id_to_path[doc_id] = note.rel_path
            self._path_to_id[note.rel_path] = doc_id
            if note.title:
                slug = slugify(note.title)
                existing_slug_id = self._slug_to_id.get(slug)
                if existing_slug_id is not None and existing_slug_id != doc_id:
                    logger.warning(
                        "Slug collision detected: slug=%r already used by %r, also claimed by %r",
                        slug,
                        existing_slug_id,
                        doc_id,
                    )
                else:
                    self._slug_to_id[slug] = doc_id
                    self._id_to_title[doc_id] = note.title

            cached = CachedMeta.from_frontmatter(
                note.frontmatter, note.rel_path, seq=self.next_seq()
            )
            self._meta_cache[doc_id] = cached
            self.index_doc(doc_id, cached)

            raw_url = note.frontmatter.get("source_url")
            if isinstance(raw_url, str) and raw_url:
                try:
                    norm = normalize_url(raw_url)
                    if norm not in self._source_url_to_id:
                        self._source_url_to_id[norm] = doc_id
                    else:
                        collisions.append((norm, self._source_url_to_id[norm], doc_id))
                except ValueError:
                    pass  # Skip invalid URLs on load

            derived_from = note.frontmatter.get("derived_from_ids", [])
            deferred_provenance.append(
                (doc_id, derived_from if isinstance(derived_from, list) else [])
            )

        # Pass 2: normalize and classify provenance now that all ids are known.
        for doc_id, source_ids in deferred_provenance:
            normalized_ids = normalize_derived_from_ids_lenient(source_ids, self_id=doc_id)
            self._classify_provenance(doc_id, normalized_ids)

        resolved_count = sum(len(v) for v in self._source_to_derived.values())
        unresolved_count = sum(len(v) for v in self._unresolved_provenance.values())
        if resolved_count or unresolved_count:
            logger.info(
                "Provenance scan: %d resolved references, %d unresolved references",
                resolved_count,
                unresolved_count,
            )

        # Report collisions deterministically (sorted by normalized URL).
        if collisions:
            collisions.sort(key=lambda t: t[0])
            self.duplicate_url_count = len(collisions)
            for norm_url, first_id, dup_id in collisions:
                logger.warning(
                    "Duplicate source_url at startup: %s owned by %s, duplicate in %s (skipped)",
                    norm_url,
                    first_id,
                    dup_id,
                )

    # ==================== Read accessors ====================

    def get_cached_meta(self, node_id: str) -> CachedMeta | None:
        """Return cached metadata for a node, or ``None`` if unknown."""
        return self._meta_cache.get(node_id)

    def iter_cached_meta(self) -> Iterable[tuple[str, CachedMeta]]:
        """Snapshot ``(node_id, cached_meta)`` pairs for safe iteration."""
        return list(self._meta_cache.items())

    @property
    def document_count(self) -> int:
        """Count of known documents (from in-memory cache)."""
        return len(self._meta_cache)

    @property
    def stale_document_count(self) -> int:
        """Count of documents whose expires_at is set and in the past."""
        now = datetime.now(UTC)
        count = 0
        for cached in self._meta_cache.values():
            if cached.expires_at is not None and normalize_datetime(cached.expires_at) < now:
                count += 1
        return count

    def id_by_slug(self, slug: str) -> str | None:
        """Get document id by slug."""
        return self._slug_to_id.get(slug)

    def id_by_relpath(self, rel_path: Path) -> str | None:
        """Get document id by an already-resolved relative path."""
        return self._path_to_id.get(rel_path)

    def relpath_of(self, doc_id: str) -> Path | None:
        """Get a document's relative path, or ``None`` if unknown."""
        return self._id_to_path.get(doc_id)

    def all_slugs(self) -> dict[str, str]:
        """Snapshot the slug→id map."""
        return dict(self._slug_to_id)

    def title_by_id(self, doc_id: str) -> str:
        """Get document title by id, empty string if unknown."""
        return self._id_to_title.get(doc_id, "")

    def has_document(self, doc_id: str) -> bool:
        """Whether a document id exists."""
        return doc_id in self._id_to_path

    def iter_doc_ids(self) -> Iterable[str]:
        """Snapshot every known document id."""
        return list(self._id_to_path)

    def id_by_source_url(self, norm_url: str) -> str | None:
        """Get document id owning a normalized source URL."""
        return self._source_url_to_id.get(norm_url)

    def iter_source_urls(self) -> Iterable[tuple[str, str]]:
        """Snapshot ``(normalized_url, doc_id)`` pairs."""
        return list(self._source_url_to_id.items())

    def doc_sources(self, doc_id: str) -> list[str]:
        """Get the source ids this document derives from."""
        return self._doc_to_sources.get(doc_id, [])

    def iter_doc_sources(self) -> Iterable[tuple[str, list[str]]]:
        """Snapshot ``(doc_id, source_ids)`` pairs; lists are fresh copies."""
        return [(doc_id, list(sources)) for doc_id, sources in self._doc_to_sources.items()]

    def derived_docs(self, doc_id: str) -> set[str]:
        """Get ids of documents derived from this document."""
        return self._source_to_derived.get(doc_id, set())

    def unresolved_sources(self, doc_id: str) -> list[str]:
        """Get the unresolved source ids referenced by a document."""
        sources = self._doc_to_sources.get(doc_id, [])
        return [
            sid
            for sid in sources
            if sid in self._unresolved_provenance or sid not in self._id_to_path
        ]

    def has_unresolved_source(self, source_id: str) -> bool:
        """Whether any document has an unresolved reference to ``source_id``."""
        return source_id in self._unresolved_provenance

    def provenance_neighbours(
        self, start_id: str, direction: Literal["sources", "derived"], depth: int
    ) -> list[dict[str, str]]:
        """BFS over the in-memory ``derived_from`` maps from *start_id*.

        ``direction='sources'`` walks the edges *start_id* derives from, guarding
        that each hop resolves to a known document; ``direction='derived'`` walks
        the documents derived from *start_id*. *depth* is the maximum hop count
        (callers clamp it to 1-3). *start_id* is excluded; the result is sorted by
        id with titles resolved, so the set iteration in the ``derived`` direction
        stays deterministic.
        """
        visited: set[str] = {start_id}
        frontier: collections.deque[str] = collections.deque()

        # Seed the frontier with immediate neighbours.
        if direction == "sources":
            for nid in self.doc_sources(start_id):
                if self.has_document(nid) and nid not in visited:
                    frontier.append(nid)
                    visited.add(nid)
        else:  # "derived"
            for nid in self.derived_docs(start_id):
                if nid not in visited:
                    frontier.append(nid)
                    visited.add(nid)

        current_depth = 1
        result_ids: list[str] = list(frontier)

        while current_depth < depth and frontier:
            next_frontier: list[str] = []
            for node_id in frontier:
                if direction == "sources":
                    for nid in self.doc_sources(node_id):
                        if self.has_document(nid) and nid not in visited:
                            next_frontier.append(nid)
                            visited.add(nid)
                else:
                    for nid in self.derived_docs(node_id):
                        if nid not in visited:
                            next_frontier.append(nid)
                            visited.add(nid)
            frontier = collections.deque(next_frontier)
            result_ids.extend(next_frontier)
            current_depth += 1

        return sorted(
            [{"id": nid, "title": self.title_by_id(nid)} for nid in result_ids],
            key=lambda n: n["id"],
        )

    def all_tags(self) -> dict[str, int]:
        """Tag → document-count over the whole cache."""
        tag_counts: dict[str, int] = {}
        for cached in self._meta_cache.values():
            for tag in cached.tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        return tag_counts
