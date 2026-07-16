"""Knowledge module - Markdown document CRUD with frontmatter.

The corpus *file format* lives next door in :mod:`lithos.frontmatter_codec`;
this module owns the corpus *store* — CRUD, the metadata cache and its inverted
indexes, slug/path allocation, and the reconcile seam that rebuilds the derived
views (ADR-0001). It reads and writes bytes; the codec turns those bytes into
documents and back.
"""

import asyncio
import contextlib
import logging
import os
import tempfile
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import frontmatter

from lithos._merge import merge_metadata
from lithos.config import LithosConfig
from lithos.errors import CorpusScanError, SlugCollisionError
from lithos.frontmatter_codec import (
    KnowledgeDocument,
    KnowledgeMetadata,
    canonical_metadata_value,
    decode,
    derive_namespace,
    encode,
    extract_extra,
    normalize_datetime,
    normalize_derived_from_ids_lenient,
    normalize_url,
    parse_wiki_links,
    slugify,
    truncate_content,
    validate_confidence,
    validate_derived_from_ids,
    validate_extra_metadata,
)
from lithos.telemetry import lithos_metrics, timed_write, traced

if TYPE_CHECKING:
    from lithos.graph import (
        GraphReconcilePlan,
        GraphReconcileResult,
        KnowledgeGraph,
    )
    from lithos.provenance import (
        ProvenancePlan,
        ProvenanceProjection,
        ProvenanceResult,
    )
    from lithos.search import (
        IndexableDocument,
        SearchEngine,
        SearchReconcilePlan,
        SearchReconcileResult,
    )

logger = logging.getLogger(__name__)


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically using write-then-rename."""
    tmp_fd, tmp_path_str = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path_str, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path_str)
        raise


@dataclass
class DuplicateInfo:
    """Information about a duplicate document."""

    id: str
    title: str
    source_url: str | None = None


@dataclass
class WriteResult:
    """Structured result type for create/update operations.

    Error outcomes use the error code as the canonical ``status`` value
    (``invalid_input`` / ``version_conflict`` / ``content_too_large`` /
    ``path_collision``) rather than ``status="error"`` plus a separate
    discriminator field. The plain ``"error"`` status remains as a generic
    fallback for unforeseen failures and ``slug_collision`` is raised as a
    ``SlugCollisionError`` exception (not represented in WriteResult).

    Per ``docs/plans/unified-write-contract.md``: ``"duplicate"`` is specific
    to source URL dedup; filesystem-level conflicts use ``"path_collision"``
    and carry the existing doc's id in ``path_collision_existing_id``,
    mirroring ``slug_collision_existing_id`` on the intake-layer outcome.
    """

    status: Literal[
        "created",
        "updated",
        "duplicate",
        "error",
        "invalid_input",
        "version_conflict",
        "content_too_large",
        "path_collision",
    ]
    document: KnowledgeDocument | None = None
    warnings: list[str] = field(default_factory=list)
    message: str | None = None
    duplicate_of: DuplicateInfo | None = None
    current_version: int | None = None
    path_collision_existing_id: str | None = None


@dataclass
class _CachedMeta:
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


class _UnsetType:
    """Sentinel type for omit-vs-clear distinction on optional fields."""

    _instance: "_UnsetType | None" = None

    def __new__(cls) -> "_UnsetType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance


_UNSET = _UnsetType()
"""Sentinel for omit-vs-clear distinction on optional fields."""


@dataclass(frozen=True)
class ReconcilePlan:
    """Aggregate reconcile plan owned by :class:`KnowledgeManager`.

    Carries one slice per derived view. Each slice is populated when the
    corresponding engine is passed to :meth:`KnowledgeManager.plan_reconcile`.
    """

    search: "SearchReconcilePlan | None" = None
    graph: "GraphReconcilePlan | None" = None
    provenance: "ProvenancePlan | None" = None


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of applying a :class:`ReconcilePlan`."""

    search: "SearchReconcileResult | None" = None
    graph: "GraphReconcileResult | None" = None
    provenance: "ProvenanceResult | None" = None


class KnowledgeManager:
    """Manages knowledge documents - CRUD operations."""

    @staticmethod
    def to_indexable(doc: KnowledgeDocument) -> "IndexableDocument":
        """Translate a :class:`KnowledgeDocument` into the search seam type.

        The single point where ``None`` values, ``Path`` objects, and
        list-typed tags are coerced into the seam's all-string form. After
        this translation, no ``KnowledgeDocument`` shape crosses into
        :class:`~lithos.search.SearchEngine`.
        """
        from lithos.search import IndexableDocument

        return IndexableDocument(
            id=doc.id,
            title=doc.title,
            content=doc.content,
            path=str(doc.path),
            author=doc.metadata.author,
            tags=tuple(doc.metadata.tags),
            entities=tuple(doc.metadata.entities),
            source_url=doc.metadata.source_url or "",
            updated_at=(doc.metadata.updated_at.isoformat() if doc.metadata.updated_at else ""),
            expires_at=(doc.metadata.expires_at.isoformat() if doc.metadata.expires_at else ""),
        )

    async def scan_corpus(self) -> list[KnowledgeDocument]:
        """Return every document in the authoritative markdown corpus.

        Owned by KnowledgeManager because the corpus is its source of truth.
        Reconciliation reads through here; it never writes.

        Raises :class:`~lithos.errors.CorpusScanError` if any known document
        could not be read. Consumers treat this snapshot as authoritative and
        delete derived state for anything missing from it, so a short read must
        abort the scan rather than masquerade as a smaller corpus — otherwise a
        single unreadable note silently evicts its own search-index entry,
        graph node, and ``derived_from`` edges.
        """
        _, total = await self.list_all(limit=0)
        if total == 0:
            return []
        docs, _ = await self.list_all(limit=total)
        if len(docs) != total:
            raise CorpusScanError(expected=total, read=len(docs))
        return docs

    async def plan_reconcile(
        self,
        search: "SearchEngine | None" = None,
        graph: "KnowledgeGraph | None" = None,
        projection: "ProvenanceProjection | None" = None,
    ) -> ReconcilePlan:
        """Plan a reconcile of every derived view against the corpus.

        Slices are populated for the engines that are passed in.

        Propagates :class:`~lithos.errors.CorpusScanError` when the corpus
        cannot be read in full: every slice below deletes derived state for
        documents absent from *corpus*, so no plan is safer than a plan built
        from a partial snapshot.
        """
        corpus = await self.scan_corpus()
        search_plan: SearchReconcilePlan | None = None
        graph_plan: GraphReconcilePlan | None = None
        provenance_plan: ProvenancePlan | None = None
        if search is not None:
            indexables = [self.to_indexable(d) for d in corpus]
            search_plan = search.plan_reconcile_to(indexables)
        if graph is not None:
            graph_plan = graph._plan_reconcile_to(corpus)
        if projection is not None:
            provenance_plan = await projection._plan_reconcile_to(corpus)
        return ReconcilePlan(search=search_plan, graph=graph_plan, provenance=provenance_plan)

    async def apply_reconcile(
        self,
        plan: ReconcilePlan,
        search: "SearchEngine | None" = None,
        graph: "KnowledgeGraph | None" = None,
        projection: "ProvenanceProjection | None" = None,
    ) -> ReconcileResult:
        """Apply *plan* — bringing each derived view back into agreement.

        Each slice is applied via the corresponding engine when both the slice
        and engine are present; missing pairs are skipped silently so callers
        can reconcile a single view without constructing the others.
        """
        search_result: SearchReconcileResult | None = None
        graph_result: GraphReconcileResult | None = None
        provenance_result: ProvenanceResult | None = None
        if plan.search is not None and search is not None:
            search_result = search.apply_reconcile(plan.search)
        if plan.graph is not None and graph is not None:
            graph_result = graph._apply_reconcile(plan.graph)
        if plan.provenance is not None and projection is not None:
            provenance_result = await projection._apply_reconcile(plan.provenance)
        return ReconcileResult(
            search=search_result,
            graph=graph_result,
            provenance=provenance_result,
        )

    def __init__(self, config: LithosConfig):
        """Initialize knowledge manager.

        Args:
            config: LithosConfig instance.  Must be provided explicitly — no
                    global fallback.  CLI entry points should pass the result
                    of ``get_config()``; tests should construct an isolated
                    ``LithosConfig`` with a temporary ``data_dir``.
        """
        self.config = config
        self.knowledge_path = self.config.storage.knowledge_path
        self._id_to_path: dict[str, Path] = {}
        self._path_to_id: dict[Path, str] = {}
        self._slug_to_id: dict[str, str] = {}
        self._source_url_to_id: dict[str, str] = {}
        self._write_lock = asyncio.Lock()
        self.duplicate_url_count: int = 0
        # Provenance indexes
        self._doc_to_sources: dict[str, list[str]] = {}
        self._source_to_derived: dict[str, set[str]] = {}
        self._unresolved_provenance: dict[str, set[str]] = {}
        self._id_to_title: dict[str, str] = {}
        self._meta_cache: dict[str, _CachedMeta] = {}
        # Inverted indexes for sub-linear equality filtering (#306). All map a
        # value to the set of doc ids carrying it; maintained beside _meta_cache.
        self._author_index: dict[str, set[str]] = {}
        self._status_index: dict[str, set[str]] = {}
        self._tag_index: dict[str, set[str]] = {}
        self._entities_index: dict[str, set[str]] = {}
        self._metadata_index: dict[str, dict[str, set[str]]] = {}
        self._meta_seq: int = 0
        self._scan_existing()

    def _next_seq(self) -> int:
        self._meta_seq += 1
        return self._meta_seq

    def _index_doc(self, doc_id: str, cached: _CachedMeta) -> None:
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

    def _deindex_doc(self, doc_id: str, cached: _CachedMeta) -> None:
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

    def _candidate_ids(
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

    def _scan_existing(self) -> None:
        """Scan existing documents and build indices.

        Uses a two-pass approach:
        - Pass 1: Walk files, populate core indexes and collect provenance pairs.
        - Pass 2: Classify provenance references as resolved or unresolved.
        """
        # Clear all indexes before rebuilding (prevents stale accumulation).
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

        if not self.knowledge_path.exists():
            return

        base_path = self.knowledge_path.resolve()
        # Collect candidates in sorted order for deterministic first-seen-wins.
        candidates: list[tuple[Path, Path]] = []
        for md_file in self.knowledge_path.rglob("*.md"):
            resolved = md_file.resolve()
            if not resolved.is_relative_to(base_path):
                continue
            candidates.append((md_file.relative_to(self.knowledge_path), md_file))
        candidates.sort(key=lambda t: t[0])
        collisions: list[tuple[str, str, str]] = []  # (norm_url, first_id, dup_id)

        # Pass 1: Walk files, populate core indexes, collect provenance.
        deferred_provenance: list[tuple[str, list[str]]] = []

        for rel_path, md_file in candidates:
            try:
                post = frontmatter.load(str(md_file))
                doc_id: str | None = post.metadata.get("id")  # type: ignore[assignment]
                title: str = post.metadata.get("title", "")  # type: ignore[assignment]
                if doc_id:
                    self._id_to_path[doc_id] = rel_path
                    self._path_to_id[rel_path] = doc_id
                    if title:
                        slug = slugify(title)
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
                            self._id_to_title[doc_id] = title

                    # Populate metadata cache for filtering
                    raw_updated = post.metadata.get("updated_at")
                    if isinstance(raw_updated, str):
                        updated_at = datetime.fromisoformat(raw_updated)
                    elif isinstance(raw_updated, datetime):
                        updated_at = raw_updated
                    else:
                        updated_at = datetime.now(UTC)
                    raw_tags: list[str] = post.metadata.get("tags", [])  # type: ignore[assignment]
                    raw_author: str = post.metadata.get("author", "")  # type: ignore[assignment]
                    raw_expires: str | datetime | None = post.metadata.get("expires_at")  # type: ignore[assignment]
                    if isinstance(raw_expires, str):
                        try:
                            cached_expires: datetime | None = datetime.fromisoformat(raw_expires)
                        except ValueError:
                            cached_expires = None
                    elif isinstance(raw_expires, datetime):
                        cached_expires = raw_expires
                    else:
                        cached_expires = None
                    raw_access_scope: str | None = post.metadata.get("access_scope")  # type: ignore[assignment]
                    raw_source: str | None = post.metadata.get("source")  # type: ignore[assignment]
                    raw_note_type: str | None = post.metadata.get("note_type")  # type: ignore[assignment]
                    raw_namespace: str | None = post.metadata.get("namespace")  # type: ignore[assignment]
                    raw_status: str | None = post.metadata.get("status")  # type: ignore[assignment]
                    raw_source_url: str | None = post.metadata.get("source_url")  # type: ignore[assignment]
                    raw_entities: list[str] = post.metadata.get("entities", [])  # type: ignore[assignment]
                    cached_namespace = (
                        raw_namespace
                        if isinstance(raw_namespace, str) and raw_namespace
                        else derive_namespace(rel_path)
                    )
                    cached = _CachedMeta(
                        title=title,
                        author=raw_author if isinstance(raw_author, str) else "",
                        tags=raw_tags if isinstance(raw_tags, list) else [],
                        updated_at=updated_at,
                        path=rel_path,
                        namespace=cached_namespace,
                        expires_at=cached_expires,
                        access_scope=raw_access_scope
                        if isinstance(raw_access_scope, str)
                        else None,
                        source=raw_source if isinstance(raw_source, str) else None,
                        note_type=raw_note_type if isinstance(raw_note_type, str) else None,
                        status=raw_status if isinstance(raw_status, str) else None,
                        source_url=raw_source_url if isinstance(raw_source_url, str) else None,
                        entities=raw_entities if isinstance(raw_entities, list) else [],
                        extra=extract_extra(post.metadata),
                        seq=self._next_seq(),
                    )
                    self._meta_cache[doc_id] = cached
                    self._index_doc(doc_id, cached)

                    # Populate source_url -> id map
                    raw_url: str | None = post.metadata.get("source_url")  # type: ignore[assignment]
                    if raw_url:
                        try:
                            norm = normalize_url(raw_url)
                            if norm not in self._source_url_to_id:
                                self._source_url_to_id[norm] = doc_id
                            else:
                                existing_id = self._source_url_to_id[norm]
                                collisions.append((norm, existing_id, doc_id))
                        except ValueError:
                            pass  # Skip invalid URLs on load

                    # Collect derived_from_ids for pass 2
                    derived_from: list[str] = post.metadata.get("derived_from_ids", [])  # type: ignore[assignment]
                    if isinstance(derived_from, list):
                        deferred_provenance.append((doc_id, derived_from))
                    else:
                        deferred_provenance.append((doc_id, []))
            except Exception as e:
                logger.warning("Skipping invalid file %s: %s", md_file, e)

        # Pass 2: Normalize and classify provenance references as resolved or unresolved.
        for doc_id, source_ids in deferred_provenance:
            normalized_ids = normalize_derived_from_ids_lenient(source_ids, self_id=doc_id)
            self._doc_to_sources[doc_id] = normalized_ids
            for source_id in normalized_ids:
                if source_id in self._id_to_path:
                    # Resolved: source document exists
                    if source_id not in self._source_to_derived:
                        self._source_to_derived[source_id] = set()
                    self._source_to_derived[source_id].add(doc_id)
                else:
                    # Unresolved: source document not found
                    if source_id not in self._unresolved_provenance:
                        self._unresolved_provenance[source_id] = set()
                    self._unresolved_provenance[source_id].add(doc_id)

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

    def _resolve_safe_path(self, path: Path) -> tuple[Path, Path]:
        """Resolve a path under knowledge root and prevent traversal."""
        if path.is_absolute():
            raise ValueError("Path must be relative to knowledge directory")

        full_path = (self.knowledge_path / path).resolve()
        base_path = self.knowledge_path.resolve()
        if not full_path.is_relative_to(base_path):
            raise ValueError("Path must stay within knowledge directory")

        return full_path.relative_to(base_path), full_path

    @traced("lithos.knowledge.create")
    @timed_write("create")
    async def create(
        self,
        title: str,
        content: str,
        agent: str,
        tags: list[str] | None = None,
        confidence: float | None = None,
        path: str | None = None,
        source: str | None = None,
        source_url: str | None = None,
        derived_from_ids: list[str] | None = None,
        expires_at: datetime | None = None,
        schema_version: int | None = None,
        namespace: str | None = None,
        access_scope: str | None = None,
        note_type: str | None = None,
        lcma_status: str | None = None,
        summaries: dict | None = None,
        extra: dict | None = None,
    ) -> WriteResult:
        """Create a new knowledge document.

        ``confidence=None`` means "not provided" and applies the default 1.0 —
        MCP callers omit the field as ``null``. Anything else must be a finite
        number in [0.0, 1.0] or the write is rejected (#312); previously a
        ``None`` here was persisted as ``confidence: null`` in frontmatter,
        which poisoned every later read.

        ``extra`` is free-form key/value metadata persisted into the
        document's frontmatter via ``KnowledgeMetadata.extra`` (#305).

        Returns WriteResult with status 'created', 'duplicate', or 'error'.
        """
        async with self._write_lock:
            lithos_metrics.knowledge_ops.add(1, {"op": "create"})

            # Reject invalid confidence before anything is persisted (#312).
            try:
                confidence = 1.0 if confidence is None else validate_confidence(confidence)
            except ValueError as e:
                return WriteResult(status="invalid_input", message=str(e))

            # Validate and normalize source_url
            norm_url: str | None = None
            if source_url is not None:
                try:
                    norm_url = normalize_url(source_url)
                except ValueError as e:
                    return WriteResult(
                        status="invalid_input",
                        message=str(e),
                    )

                # Check dedup map
                existing_id = self._source_url_to_id.get(norm_url)
                if existing_id is not None:
                    try:
                        existing_doc, _ = await self.read(id=existing_id)
                        logger.info(
                            "Duplicate URL rejected: url=%s existing_owner=%s rejected_doc title=%r",
                            norm_url,
                            existing_id,
                            title,
                        )
                        return WriteResult(
                            status="duplicate",
                            duplicate_of=DuplicateInfo(
                                id=existing_id,
                                title=existing_doc.title,
                                source_url=norm_url,
                            ),
                            message=f"URL already exists in document '{existing_doc.title}'",
                        )
                    except FileNotFoundError:
                        # Stale map entry; allow create
                        del self._source_url_to_id[norm_url]

            # Validate and normalize derived_from_ids
            normalized_provenance: list[str] = []
            if derived_from_ids:
                try:
                    normalized_provenance = validate_derived_from_ids(derived_from_ids)
                except ValueError as e:
                    return WriteResult(
                        status="invalid_input",
                        message=str(e),
                    )

            # Guard free-form metadata against reserved-key collisions (#305).
            if extra:
                try:
                    validate_extra_metadata(extra)
                except ValueError as e:
                    return WriteResult(status="invalid_input", message=str(e))

            doc_id = str(uuid.uuid4())
            now = datetime.now(UTC)

            metadata = KnowledgeMetadata(
                id=doc_id,
                title=title,
                author=agent,
                created_at=now,
                updated_at=now,
                tags=tags or [],
                confidence=confidence,
                contributors=[],
                source=source,
                source_url=norm_url,
                derived_from_ids=normalized_provenance,
                expires_at=expires_at,
                schema_version=schema_version if schema_version is not None else 1,
                namespace=namespace,
                access_scope=access_scope if access_scope is not None else "shared",
                note_type=note_type if note_type is not None else "observation",
                status=lcma_status if lcma_status is not None else "active",
                summaries=summaries,
                extra=dict(extra) if extra else {},
            )

            # Determine file path.
            #
            # `path` semantics:
            #   - None/empty   → filename = slugify(title) + ".md" at knowledge root
            #   - ends in ".md" (final segment only) → treat as explicit relative file path;
            #                    the filename is taken verbatim, title is not slugified into it
            #   - otherwise    → treat as subdirectory; append slugify(title) + ".md"
            #
            # Any non-final path segment ending in ".md" is rejected: that shape would
            # silently create a directory whose name ends in ".md", which is what
            # confused callers into double-nesting documents (issue #300).
            slug = slugify(title)
            if not path:
                file_path = Path(f"{slug}.md")
            else:
                parts = Path(path).parts
                if any(p.endswith(".md") for p in parts[:-1]):
                    return WriteResult(
                        status="invalid_input",
                        message=(
                            f"path contains a '.md' segment that is not the final "
                            f"segment: {path!r}; directories ending in '.md' are not allowed"
                        ),
                    )
                if parts and parts[-1].endswith(".md"):
                    file_path = Path(path)
                else:
                    file_path = Path(path) / f"{slug}.md"
            file_path, full_path = self._resolve_safe_path(file_path)

            # Parse wiki-links
            links = parse_wiki_links(content)

            doc = KnowledgeDocument(
                id=doc_id,
                title=title,
                content=content,
                metadata=metadata,
                path=file_path,
                links=links,
            )

            # Check for slug collision before writing anything
            existing_slug_id = self._slug_to_id.get(slug)
            if existing_slug_id is not None and existing_slug_id != doc_id:
                raise SlugCollisionError(slug, existing_slug_id)

            # Check for explicit-path collision. In the directory-semantics
            # mode the slug check above already covers this (slug uniquely
            # determines file_path). In the explicit-`.md` mode added for
            # issue #300, two distinct titles can resolve to the same file
            # path — without this guard the second create would silently
            # overwrite the first file and leave `_id_to_path` retaining
            # both IDs pointing at one path.
            #
            # Status is "path_collision" per docs/plans/unified-write-contract.md:
            # "duplicate" is reserved for source-URL dedup; filesystem-level
            # conflicts get their own machine-readable code with the existing
            # doc id carried in `path_collision_existing_id` (mirrors how
            # `slug_collision_existing_id` works at the intake layer).
            existing_path_id = self._path_to_id.get(file_path)
            if existing_path_id is not None and existing_path_id != doc_id:
                return WriteResult(
                    status="path_collision",
                    path_collision_existing_id=existing_path_id,
                    message=(
                        f"Path {str(file_path)!r} is already used by document {existing_path_id!r}"
                    ),
                )

            # Write to disk
            full_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(full_path, encode(doc))

            # Update indices
            self._id_to_path[doc_id] = file_path
            self._path_to_id[file_path] = doc_id
            self._slug_to_id[slug] = doc_id
            if norm_url is not None:
                self._source_url_to_id[norm_url] = doc_id

            # Update provenance indexes
            warnings: list[str] = []
            self._doc_to_sources[doc_id] = normalized_provenance
            self._id_to_title[doc_id] = title
            for source_id in normalized_provenance:
                if source_id in self._id_to_path:
                    # Resolved: source document exists
                    if source_id not in self._source_to_derived:
                        self._source_to_derived[source_id] = set()
                    self._source_to_derived[source_id].add(doc_id)
                else:
                    # Unresolved: source document not found
                    if source_id not in self._unresolved_provenance:
                        self._unresolved_provenance[source_id] = set()
                    self._unresolved_provenance[source_id].add(doc_id)
                    logger.warning(
                        "Provenance resolution failed: source_id=%s dependent_doc_id=%s",
                        source_id,
                        doc_id,
                    )
                    warnings.append(f"derived_from_ids contains missing document: {source_id}")

            # Auto-resolve: check if any existing docs had unresolved refs to this new doc
            if doc_id in self._unresolved_provenance:
                resolved_docs = self._unresolved_provenance.pop(doc_id)
                if doc_id not in self._source_to_derived:
                    self._source_to_derived[doc_id] = set()
                self._source_to_derived[doc_id].update(resolved_docs)

            # Resolve namespace: explicit override if set, otherwise derive
            # from path (matches apply_lcma_defaults at read time).
            cached_namespace = metadata.namespace or derive_namespace(file_path)

            cached = _CachedMeta(
                title=title,
                author=metadata.author,
                tags=list(metadata.tags),
                updated_at=metadata.updated_at,
                path=file_path,
                namespace=cached_namespace,
                expires_at=metadata.expires_at,
                access_scope=metadata.access_scope,
                source=metadata.source,
                note_type=metadata.note_type,
                status=metadata.status,
                source_url=metadata.source_url,
                entities=list(metadata.entities),
                extra=dict(metadata.extra),
                seq=self._next_seq(),
            )
            self._meta_cache[doc_id] = cached
            self._index_doc(doc_id, cached)

            logger.info(
                "Document created: doc_id=%s title=%.60s agent=%s",
                doc_id,
                title,
                agent,
            )
            return WriteResult(status="created", document=doc, warnings=warnings)

    @traced("lithos.knowledge.read")
    async def read(
        self,
        id: str | None = None,
        path: str | None = None,
        max_length: int | None = None,
    ) -> tuple[KnowledgeDocument, bool]:
        """Read a knowledge document.

        Returns:
            Tuple of (document, was_truncated)
        """
        lithos_metrics.knowledge_ops.add(1, {"op": "read"})
        if id:
            if id not in self._id_to_path:
                raise FileNotFoundError(f"Document not found: {id}")
            file_path = self._id_to_path[id]
        elif path:
            file_path = Path(path)
            if not file_path.suffix:
                file_path = file_path.with_suffix(".md")
        else:
            raise ValueError("Must provide id or path")

        file_path, full_path = self._resolve_safe_path(file_path)
        if not full_path.exists():
            raise FileNotFoundError(f"Document not found: {file_path}")

        doc = decode(full_path.read_text(encoding="utf-8"), file_path)

        # Truncate only after decoding. The codec parses links from the whole
        # body, so an excerpt narrows the content a caller sees without changing
        # which links it reports.
        truncated = False
        if max_length:
            doc.content, truncated = truncate_content(doc.content, max_length)

        # Overlay canonical provenance from in-memory index so all callers
        # see the same normalized value (not just the raw frontmatter).
        # Only overlay when the index has a non-empty list; an empty index
        # entry means the doc was created without provenance, so the on-disk
        # frontmatter (which may have been edited externally) takes precedence.
        indexed_sources = self._doc_to_sources.get(doc.metadata.id)
        if indexed_sources:
            doc.metadata.derived_from_ids = indexed_sources

        return doc, truncated

    def _remove_provenance_entries(self, doc_id: str) -> None:
        """Remove a document's provenance entries from reverse indexes.

        Cleans up _source_to_derived and _unresolved_provenance for the given doc_id
        based on its current _doc_to_sources entries.
        """
        old_sources = self._doc_to_sources.get(doc_id, [])
        for source_id in old_sources:
            # Remove from resolved index
            if source_id in self._source_to_derived:
                self._source_to_derived[source_id].discard(doc_id)
                if not self._source_to_derived[source_id]:
                    del self._source_to_derived[source_id]
            # Remove from unresolved index
            if source_id in self._unresolved_provenance:
                self._unresolved_provenance[source_id].discard(doc_id)
                if not self._unresolved_provenance[source_id]:
                    del self._unresolved_provenance[source_id]

    @traced("lithos.knowledge.update")
    @timed_write("update")
    async def update(
        self,
        id: str,
        agent: str,
        content: str | None = None,
        title: str | None = None,
        tags: list[str] | _UnsetType = _UNSET,
        confidence: float | _UnsetType = _UNSET,
        source_url: str | None | _UnsetType = _UNSET,
        derived_from_ids: list[str] | None | _UnsetType = _UNSET,
        expires_at: datetime | None | _UnsetType = _UNSET,
        expected_version: int | None = None,
        source: str | None | _UnsetType = _UNSET,
        schema_version: int | _UnsetType = _UNSET,
        namespace: str | None | _UnsetType = _UNSET,
        access_scope: str | None | _UnsetType = _UNSET,
        note_type: str | None | _UnsetType = _UNSET,
        lcma_status: str | None | _UnsetType = _UNSET,
        summaries: dict | None | _UnsetType = _UNSET,
        supersedes: str | None | _UnsetType = _UNSET,
        entities: list[str] | None | _UnsetType = _UNSET,
        entities_extractor: int | None | _UnsetType = _UNSET,
        extra: dict | _UnsetType = _UNSET,
    ) -> WriteResult:
        """Update an existing document.

        entities/entities_extractor semantics (#313):
        - entities _UNSET: preserve both entities and extractor marker
        - entities set with entities_extractor: extractor-written provenance
        - entities set WITHOUT entities_extractor: agent-curated — the marker
          is cleared so the enrichment worker never overwrites them

        tags semantics:
        - _UNSET (default): preserve existing tags
        - []: clear all tags
        - non-empty list: replace tags

        confidence semantics:
        - _UNSET (default): preserve existing confidence
        - float: set new value

        source_url semantics:
        - _UNSET (default): preserve existing source_url, no map change
        - None: clear existing source_url, remove from map
        - str: normalize, allow if same doc owns it, reject if different doc owns it

        derived_from_ids semantics:
        - _UNSET (default): preserve existing derived_from_ids, no index change
        - None or []: clear existing provenance, remove from all provenance indexes
        - non-empty list: validate, normalize, replace entire set

        expires_at semantics:
        - _UNSET (default): preserve existing expires_at
        - None: clear existing expires_at
        - datetime: set new value

        extra semantics (#305) — free-form key/value metadata:
        - _UNSET (default): preserve existing metadata
        - {}: clear all metadata
        - non-empty dict: additive per-key merge (a key whose value is None
          deletes that key; other keys are set; absent keys are preserved)

        Note: version is incremented on every call, even when no fields actually change.
        This is intentional — simplicity over precision. Callers should not rely on
        version stability as a proxy for content equality.
        """
        async with self._write_lock:
            lithos_metrics.knowledge_ops.add(1, {"op": "update"})
            doc, _ = await self.read(id=id)

            # Guard free-form metadata against reserved-key collisions (#305).
            # {} (clear) has no keys to check; _UNSET means preserve.
            if not isinstance(extra, _UnsetType) and extra:
                try:
                    validate_extra_metadata(extra)
                except ValueError as e:
                    return WriteResult(status="invalid_input", message=str(e))

            # Reject invalid confidence before any in-memory metadata mutation —
            # update() mutates the cached doc, so a late rejection would leave a
            # poisoned in-memory document (#312).
            if not isinstance(confidence, _UnsetType):
                try:
                    confidence = validate_confidence(confidence)
                except ValueError as e:
                    return WriteResult(status="invalid_input", message=str(e))

            # Validate task-scope invariant under the write lock so the
            # source-existence check is atomic with the write. See
            # ADR-0003: pre-reading the document at the handler layer is a
            # TOCTOU window — another writer could rename the source between
            # the read and the update.
            if access_scope == "task":
                effective_source: str | None | _UnsetType = (
                    doc.metadata.source if isinstance(source, _UnsetType) else source
                )
                if not effective_source:
                    return WriteResult(
                        status="invalid_input",
                        message="access_scope='task' requires source_task",
                    )

            if expected_version is not None and doc.metadata.version != expected_version:
                logger.warning(
                    "Version conflict: doc_id=%s expected_version=%d actual_version=%d",
                    id,
                    expected_version,
                    doc.metadata.version,
                )
                return WriteResult(
                    status="version_conflict",
                    message=f"Version conflict: expected {expected_version}, got {doc.metadata.version}",
                    current_version=doc.metadata.version,
                )

            old_slug = slugify(doc.metadata.title)
            old_source_url = doc.metadata.source_url

            # Guard: check slug collision BEFORE any state mutations.
            # If a title rename would collide, bail out immediately so that
            # source_url / provenance mutations further down never run.
            if title is not None:
                new_slug = slugify(title)
                if new_slug != old_slug:
                    existing_owner = self._slug_to_id.get(new_slug)
                    if existing_owner is not None and existing_owner != id:
                        raise SlugCollisionError(new_slug, existing_owner)

            # Handle source_url update
            if not isinstance(source_url, _UnsetType):
                if source_url is None:
                    # Clear source_url
                    if old_source_url:
                        try:
                            old_norm = normalize_url(old_source_url)
                            if self._source_url_to_id.get(old_norm) == id:
                                del self._source_url_to_id[old_norm]
                        except ValueError:
                            pass
                    doc.metadata.source_url = None
                else:
                    # Set/change source_url
                    try:
                        new_norm = normalize_url(source_url)
                    except ValueError as e:
                        return WriteResult(
                            status="invalid_input",
                            message=str(e),
                        )

                    existing_owner = self._source_url_to_id.get(new_norm)
                    if existing_owner is not None and existing_owner != id:
                        try:
                            existing_doc, _ = await self.read(id=existing_owner)
                            logger.info(
                                "Duplicate URL rejected: url=%s existing_owner=%s rejected_doc_id=%s",
                                new_norm,
                                existing_owner,
                                id,
                            )
                            return WriteResult(
                                status="duplicate",
                                duplicate_of=DuplicateInfo(
                                    id=existing_owner,
                                    title=existing_doc.title,
                                    source_url=new_norm,
                                ),
                                message=f"URL already exists in document '{existing_doc.title}'",
                            )
                        except FileNotFoundError:
                            del self._source_url_to_id[new_norm]

                    # Remove old mapping if URL changed
                    if old_source_url:
                        try:
                            old_norm = normalize_url(old_source_url)
                            if old_norm != new_norm and self._source_url_to_id.get(old_norm) == id:
                                del self._source_url_to_id[old_norm]
                        except ValueError:
                            pass

                    doc.metadata.source_url = new_norm
                    self._source_url_to_id[new_norm] = id

            # Handle derived_from_ids update
            warnings: list[str] = []
            if not isinstance(derived_from_ids, _UnsetType):
                if derived_from_ids is None or derived_from_ids == []:
                    # Clear provenance
                    self._remove_provenance_entries(id)
                    doc.metadata.derived_from_ids = []
                    self._doc_to_sources[id] = []
                else:
                    # Replace with new list — validate first
                    try:
                        normalized = validate_derived_from_ids(derived_from_ids, self_id=id)
                    except ValueError as e:
                        return WriteResult(
                            status="invalid_input",
                            message=str(e),
                        )

                    # Remove old provenance entries
                    self._remove_provenance_entries(id)

                    # Add new entries
                    doc.metadata.derived_from_ids = normalized
                    self._doc_to_sources[id] = normalized
                    for source_id in normalized:
                        if source_id in self._id_to_path:
                            if source_id not in self._source_to_derived:
                                self._source_to_derived[source_id] = set()
                            self._source_to_derived[source_id].add(id)
                        else:
                            if source_id not in self._unresolved_provenance:
                                self._unresolved_provenance[source_id] = set()
                            self._unresolved_provenance[source_id].add(id)
                            logger.warning(
                                "Provenance resolution failed: source_id=%s dependent_doc_id=%s",
                                source_id,
                                id,
                            )
                            warnings.append(
                                f"derived_from_ids contains missing document: {source_id}"
                            )

            # Handle expires_at update
            if not isinstance(expires_at, _UnsetType):
                doc.metadata.expires_at = expires_at

            # Handle source (task) update
            if not isinstance(source, _UnsetType):
                doc.metadata.source = source

            # Handle supersedes update
            if not isinstance(supersedes, _UnsetType):
                doc.metadata.supersedes = supersedes

            # Handle LCMA field updates — preserve existing when _UNSET
            if not isinstance(schema_version, _UnsetType):
                doc.metadata.schema_version = schema_version
            elif doc.metadata.schema_version is None:
                doc.metadata.schema_version = 1
            if not isinstance(namespace, _UnsetType):
                doc.metadata.namespace = namespace
            # namespace: no default on update — derived at read time
            if not isinstance(access_scope, _UnsetType):
                doc.metadata.access_scope = access_scope
            elif doc.metadata.access_scope is None:
                doc.metadata.access_scope = "shared"
            if not isinstance(note_type, _UnsetType):
                doc.metadata.note_type = note_type
            elif doc.metadata.note_type is None:
                doc.metadata.note_type = "observation"
            if not isinstance(lcma_status, _UnsetType):
                doc.metadata.status = lcma_status
            elif doc.metadata.status is None:
                doc.metadata.status = "active"
            if not isinstance(summaries, _UnsetType):
                doc.metadata.summaries = summaries
            if not isinstance(entities, _UnsetType):
                doc.metadata.entities = entities if entities is not None else []
                # Writing entities without extractor provenance marks them as
                # agent-curated; the enrichment worker never overwrites those.
                doc.metadata.entities_extractor = (
                    None if isinstance(entities_extractor, _UnsetType) else entities_extractor
                )
            if not isinstance(extra, _UnsetType):
                # {} clears all metadata; a non-empty dict is an additive
                # per-key merge into the existing extra (mirrors task metadata).
                doc.metadata.extra = (
                    {} if extra == {} else merge_metadata(doc.metadata.extra, extra)
                )

            # Update fields
            if content is not None:
                doc.content = content
                doc.links = parse_wiki_links(content)
            if title is not None:
                doc.title = title
                doc.metadata.title = title
            if not isinstance(tags, _UnsetType):
                doc.metadata.tags = tags
            if not isinstance(confidence, _UnsetType):
                doc.metadata.confidence = confidence

            # Update metadata
            doc.metadata.updated_at = datetime.now(UTC)
            if agent not in doc.metadata.contributors and agent != doc.metadata.author:
                doc.metadata.contributors.append(agent)

            # Slug collision was already checked at the top of update();
            # recompute new_slug from the (possibly updated) title for the
            # index-update that follows.
            new_slug = slugify(doc.metadata.title)

            # Write to disk — bump version here so early returns above leave
            # the in-memory document at its original version.
            doc.metadata.version += 1
            _safe_path, full_path = self._resolve_safe_path(doc.path)
            _atomic_write(full_path, encode(doc))

            if new_slug != old_slug:
                if self._slug_to_id.get(old_slug) == id:
                    del self._slug_to_id[old_slug]
                self._slug_to_id[new_slug] = id

            # Update _id_to_title if title changed
            if title is not None:
                self._id_to_title[id] = title

            # Update metadata cache + inverted index. Deindex the previous entry
            # first, then index the new one; preserve the insertion ordinal so
            # list ordering/pagination is unchanged by an update.
            cached_namespace = doc.metadata.namespace or derive_namespace(doc.path)
            old_cached = self._meta_cache.get(id)
            if old_cached is not None:
                self._deindex_doc(id, old_cached)
            cached = _CachedMeta(
                title=doc.metadata.title,
                author=doc.metadata.author,
                tags=list(doc.metadata.tags),
                updated_at=doc.metadata.updated_at,
                path=doc.path,
                namespace=cached_namespace,
                expires_at=doc.metadata.expires_at,
                access_scope=doc.metadata.access_scope,
                source=doc.metadata.source,
                note_type=doc.metadata.note_type,
                status=doc.metadata.status,
                source_url=doc.metadata.source_url,
                entities=list(doc.metadata.entities),
                extra=dict(doc.metadata.extra),
                seq=old_cached.seq if old_cached is not None else self._next_seq(),
            )
            self._meta_cache[id] = cached
            self._index_doc(id, cached)

            if logger.isEnabledFor(logging.INFO):
                changed: list[str] = []
                if content is not None:
                    changed.append("content")
                if title is not None:
                    changed.append("title")
                if not isinstance(tags, _UnsetType):
                    changed.append("tags")
                logger.info(
                    "Document updated: doc_id=%s agent=%s changed=%s",
                    id,
                    agent,
                    changed or ["metadata"],
                )
            return WriteResult(status="updated", document=doc, warnings=warnings)

    @traced("lithos.knowledge.delete")
    async def delete(self, id: str) -> tuple[bool, str]:
        """Delete a document.

        Returns:
            Tuple of (success, relative_path). Path is empty string if not found.
        """
        async with self._write_lock:
            lithos_metrics.knowledge_ops.add(1, {"op": "delete"})
            if id not in self._id_to_path:
                return False, ""

            # Read doc to get source_url before deleting
            try:
                doc, _ = await self.read(id=id)
                if doc.metadata.source_url:
                    try:
                        norm = normalize_url(doc.metadata.source_url)
                        if self._source_url_to_id.get(norm) == id:
                            del self._source_url_to_id[norm]
                    except ValueError:
                        pass
            except FileNotFoundError:
                pass

            file_path = self._id_to_path[id]
            _safe_path, full_path = self._resolve_safe_path(file_path)

            if full_path.exists():
                full_path.unlink()

            # Update indices
            old_path = self._id_to_path.pop(id)
            self._path_to_id.pop(old_path, None)
            # Remove from slug index
            self._slug_to_id = {k: v for k, v in self._slug_to_id.items() if v != id}

            # Provenance cleanup
            # 1. Remove this doc as a "derived" doc from reverse indexes
            self._remove_provenance_entries(id)
            # 2. Remove forward index entry
            self._doc_to_sources.pop(id, None)
            # 3. If this doc was a source for others, move those to unresolved
            derived_docs = self._source_to_derived.pop(id, set())
            if derived_docs:
                self._unresolved_provenance[id] = derived_docs
            # 4. Remove from title and metadata caches + inverted index
            self._id_to_title.pop(id, None)
            removed = self._meta_cache.pop(id, None)
            if removed is not None:
                self._deindex_doc(id, removed)

            logger.info("Document deleted: doc_id=%s path=%s", id, file_path)
            return True, str(file_path)

    @traced("lithos.knowledge.list_all")
    async def list_all(
        self,
        path_prefix: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        tags: list[str] | None = None,
        author: str | None = None,
        exclude_status: list[str] | None = None,
        metadata_match: dict | None = None,
        entities: list[str] | None = None,
    ) -> tuple[list[KnowledgeDocument], int]:
        """List all documents with optional filtering.

        Uses the in-memory metadata cache for filtering so only matching
        documents require a full disk read.

        ``exclude_status`` filters out documents whose cached status is in
        the given list (e.g. ``['quarantined']``).

        ``metadata_match`` filters by free-form metadata (#306): each ``key: q``
        matches docs whose stored value equals ``q`` or is a list containing it.

        ``entities`` filters by entity name, AND across the list (#316).

        Equality filters (``tags``, ``author``, ``metadata_match``,
        ``entities``) are resolved through inverted indexes to a candidate
        set, so a filtered query never scans the whole cache;
        ``path_prefix``/``since`` then refine only those candidates. With no
        equality filter, falls back to a full scan (which is unavoidable for
        unfiltered / prefix-only / since-only listings).
        """
        normalized_since = normalize_datetime(since) if since else None
        candidate_ids = self._candidate_ids(
            tags=tags,
            author=author,
            metadata_match=metadata_match,
            exclude_status=exclude_status,
            entities=entities,
        )

        if candidate_ids is None:
            # No equality filter — full scan (existing behaviour + ordering).
            matching_ids: list[str] = []
            for doc_id, cached in self._meta_cache.items():
                if exclude_status and cached.status in exclude_status:
                    continue
                if path_prefix and not str(cached.path).startswith(path_prefix):
                    continue
                if normalized_since and normalize_datetime(cached.updated_at) < normalized_since:
                    continue
                matching_ids.append(doc_id)
        else:
            # Index path — refine the (small) candidate set, then restore the
            # _meta_cache insertion order via the stored seq for stable paging.
            refined: list[_CachedMeta] = []
            refined_ids: list[str] = []
            for doc_id in candidate_ids:
                cached = self._meta_cache.get(doc_id)
                if cached is None:
                    continue
                if path_prefix and not str(cached.path).startswith(path_prefix):
                    continue
                if normalized_since and normalize_datetime(cached.updated_at) < normalized_since:
                    continue
                refined.append(cached)
                refined_ids.append(doc_id)
            order = sorted(range(len(refined_ids)), key=lambda i: refined[i].seq)
            matching_ids = [refined_ids[i] for i in order]

        total = len(matching_ids)
        docs = []
        for doc_id in matching_ids[offset : offset + limit]:
            try:
                doc, _ = await self.read(id=doc_id)
                docs.append(doc)
            except Exception:
                # One unreadable note must not break a listing, so the doc is
                # skipped — but never silently: `scan_corpus` turns the
                # resulting short read into a hard CorpusScanError, and this is
                # the only place the underlying cause is recoverable.
                logger.warning(
                    "list_all: skipping unreadable document %s",
                    doc_id,
                    exc_info=True,
                    extra={"doc_id": doc_id},
                )

        logger.debug(
            "list_all: total=%d returned=%d offset=%d limit=%d",
            total,
            len(docs),
            offset,
            limit,
            extra={"total": total, "returned": len(docs), "offset": offset, "limit": limit},
        )
        return docs, total

    def metadata_candidate_ids(self, metadata_match: dict | None) -> set[str] | None:
        """Public wrapper: candidate ids for a ``metadata_match`` filter (#306).

        Returns ``None`` when ``metadata_match`` is empty/None (no filtering),
        otherwise the set of doc ids matching every key (sub-linear, index-based).
        Used by the content-query path of ``lithos_list`` to intersect with
        full-text hits without scanning the cache.
        """
        if not metadata_match:
            return None
        return self._candidate_ids(
            tags=None, author=None, metadata_match=metadata_match, exclude_status=None
        )

    def entities_candidate_ids(self, entities: list[str] | None) -> set[str] | None:
        """Public wrapper: candidate ids for an ``entities`` filter (#316).

        Returns ``None`` when ``entities`` is empty/None (no filtering),
        otherwise the set of doc ids carrying every named entity (sub-linear,
        index-based). Used by ``lithos_search`` to post-filter hits without
        scanning the cache.
        """
        if not entities:
            return None
        return self._candidate_ids(
            tags=None, author=None, metadata_match=None, exclude_status=None, entities=entities
        )

    async def get_all_tags(self) -> dict[str, int]:
        """Get all tags with document counts (from in-memory cache)."""
        tag_counts: dict[str, int] = {}
        for cached in self._meta_cache.values():
            for tag in cached.tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        return tag_counts

    async def find_by_source_url(self, url: str) -> KnowledgeDocument | None:
        """Look up a document by source URL (internal only, not MCP-exposed).

        Normalizes the input URL before lookup. Does not acquire _write_lock
        (read-only on the map).
        """
        try:
            norm = normalize_url(url)
        except ValueError:
            return None

        doc_id = self._source_url_to_id.get(norm)
        if doc_id is None:
            return None

        try:
            doc, _ = await self.read(id=doc_id)
            return doc
        except FileNotFoundError:
            return None

    async def sync_from_disk(self, path: Path) -> KnowledgeDocument:
        """Re-read a file from disk and update all manager indexes.

        Handles both new files and modified files uniformly.
        Returns the parsed document for downstream search/graph indexing.

        Args:
            path: Relative path under knowledge_path (e.g. Path("my-note.md"))

        Raises:
            FileNotFoundError: If the file does not exist on disk.
            ValueError: If the file cannot be parsed.
        """
        async with self._write_lock:
            return self._sync_from_disk_unlocked(path)

    def _sync_from_disk_unlocked(self, path: Path) -> KnowledgeDocument:
        """Internal sync logic, called with _write_lock held."""
        file_path, full_path = self._resolve_safe_path(path)
        if not full_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        doc = decode(full_path.read_text(encoding="utf-8"), file_path)
        metadata = doc.metadata
        title = doc.title

        doc_id = doc.id
        is_new = doc_id not in self._id_to_path

        # Update core indexes
        if not is_new:
            old_path = self._id_to_path.get(doc_id)
            if old_path is not None:
                self._path_to_id.pop(old_path, None)
        self._id_to_path[doc_id] = file_path
        self._path_to_id[file_path] = doc_id
        old_slug = None
        if not is_new:
            # Find the old slug for this doc to clean it up
            for s, sid in self._slug_to_id.items():
                if sid == doc_id:
                    old_slug = s
                    break
        new_slug = slugify(title)
        if old_slug and old_slug != new_slug and self._slug_to_id.get(old_slug) == doc_id:
            del self._slug_to_id[old_slug]
        self._slug_to_id[new_slug] = doc_id

        # Update source_url index
        raw_url = metadata.source_url
        if raw_url:
            try:
                norm = normalize_url(raw_url)
                # Remove any old mapping for this doc
                old_urls_to_remove = [k for k, v in self._source_url_to_id.items() if v == doc_id]
                for k in old_urls_to_remove:
                    del self._source_url_to_id[k]
                # Check if another doc already owns this URL (first-owner-wins)
                existing_owner = self._source_url_to_id.get(norm)
                if existing_owner is not None and existing_owner != doc_id:
                    logger.warning(
                        "source_url collision in sync_from_disk: %s owned by %s, "
                        "skipping assignment for %s",
                        norm,
                        existing_owner,
                        doc_id,
                    )
                else:
                    self._source_url_to_id[norm] = doc_id
            except ValueError:
                pass
        else:
            # Clear any old source_url mapping for this doc
            old_urls_to_remove = [k for k, v in self._source_url_to_id.items() if v == doc_id]
            for k in old_urls_to_remove:
                del self._source_url_to_id[k]

        # Update _id_to_title
        self._id_to_title[doc_id] = title

        # Update provenance indexes
        new_sources = normalize_derived_from_ids_lenient(
            metadata.derived_from_ids or [], self_id=doc_id
        )

        if not is_new:
            # Modified file: diff against current state
            old_sources = self._doc_to_sources.get(doc_id, [])
            if old_sources != new_sources:
                # Remove old reverse index entries
                self._remove_provenance_entries(doc_id)
                # Add new entries
                self._doc_to_sources[doc_id] = list(new_sources)
                for source_id in new_sources:
                    if source_id in self._id_to_path:
                        if source_id not in self._source_to_derived:
                            self._source_to_derived[source_id] = set()
                        self._source_to_derived[source_id].add(doc_id)
                    else:
                        if source_id not in self._unresolved_provenance:
                            self._unresolved_provenance[source_id] = set()
                        self._unresolved_provenance[source_id].add(doc_id)
        else:
            # New file: add provenance entries
            self._doc_to_sources[doc_id] = list(new_sources)
            for source_id in new_sources:
                if source_id in self._id_to_path:
                    if source_id not in self._source_to_derived:
                        self._source_to_derived[source_id] = set()
                    self._source_to_derived[source_id].add(doc_id)
                else:
                    if source_id not in self._unresolved_provenance:
                        self._unresolved_provenance[source_id] = set()
                    self._unresolved_provenance[source_id].add(doc_id)

            # Auto-resolve: check if any existing docs had unresolved refs to this new doc
            if doc_id in self._unresolved_provenance:
                resolved_docs = self._unresolved_provenance.pop(doc_id)
                if doc_id not in self._source_to_derived:
                    self._source_to_derived[doc_id] = set()
                self._source_to_derived[doc_id].update(resolved_docs)

        # Update metadata cache + inverted index (deindex prior entry first;
        # preserve insertion ordinal so list ordering stays stable).
        cached_namespace = metadata.namespace or derive_namespace(file_path)
        old_cached = self._meta_cache.get(doc_id)
        if old_cached is not None:
            self._deindex_doc(doc_id, old_cached)
        cached = _CachedMeta(
            title=title,
            author=metadata.author,
            tags=list(metadata.tags),
            updated_at=metadata.updated_at,
            path=file_path,
            namespace=cached_namespace,
            expires_at=metadata.expires_at,
            access_scope=metadata.access_scope,
            source=metadata.source,
            note_type=metadata.note_type,
            status=metadata.status,
            source_url=metadata.source_url,
            entities=list(metadata.entities),
            extra=dict(metadata.extra),
            seq=old_cached.seq if old_cached is not None else self._next_seq(),
        )
        self._meta_cache[doc_id] = cached
        self._index_doc(doc_id, cached)

        return doc

    def get_id_by_slug(self, slug: str) -> str | None:
        """Get document ID by slug."""
        return self._slug_to_id.get(slug)

    def get_id_by_path(self, path: str | Path) -> str | None:
        """Get document ID by relative/absolute path (O(1) via reverse map)."""
        candidate = Path(path)

        if candidate.is_absolute():
            try:
                candidate = candidate.resolve().relative_to(self.knowledge_path.resolve())
            except ValueError:
                return None

        if not candidate.suffix:
            candidate = candidate.with_suffix(".md")

        return self._path_to_id.get(candidate)

    def get_all_slugs(self) -> dict[str, str]:
        """Get mapping of all slugs to IDs."""
        return dict(self._slug_to_id)

    # ==================== Public Provenance Accessors ====================

    def get_doc_sources(self, doc_id: str) -> list[str]:
        """Get the source IDs this document derives from."""
        return self._doc_to_sources.get(doc_id, [])

    def iter_doc_sources(self) -> Iterable[tuple[str, list[str]]]:
        """Iterate over ``(doc_id, source_ids)`` pairs for every known document.

        Snapshots the underlying ``_doc_to_sources`` index so callers can
        iterate safely even if the cache is mutated concurrently (rare, but
        possible via concurrent writes / file-watcher projections). Returned
        lists are fresh copies — mutations to them never leak back into the
        manager.

        This is the public bulk-read counterpart to :meth:`get_doc_sources`
        (issue #264) so callers don't reach into ``_doc_to_sources`` directly.
        Now used only by provenance conformance tests to cross-check KM's
        in-memory sources against the projected edges — the former full-sweep
        helper that consumed it moved into ``ProvenanceProjection`` (task
        681ac952 PR1c).
        """
        return [(doc_id, list(sources)) for doc_id, sources in self._doc_to_sources.items()]

    def get_derived_docs(self, doc_id: str) -> set[str]:
        """Get IDs of documents derived from this document."""
        return self._source_to_derived.get(doc_id, set())

    def get_unresolved_sources(self, doc_id: str) -> list[str]:
        """Get unresolved source IDs for a document."""
        sources = self._doc_to_sources.get(doc_id, [])
        return [
            sid
            for sid in sources
            if sid in self._unresolved_provenance or sid not in self._id_to_path
        ]

    def get_title_by_id(self, doc_id: str) -> str:
        """Get document title by ID, returning empty string if unknown."""
        return self._id_to_title.get(doc_id, "")

    def has_document(self, doc_id: str) -> bool:
        """Check whether a document ID exists."""
        return doc_id in self._id_to_path

    def get_cached_meta(self, node_id: str) -> _CachedMeta | None:
        """Return cached metadata for a node, or ``None`` if unknown.

        This is the public read accessor for the internal ``_meta_cache`` dict.
        Callers outside ``KnowledgeManager`` — notably the LCMA scout, rerank,
        reinforcement, and enrich paths, plus the MCP server's feedback and
        node_stats handlers — should use this rather than touching
        ``_meta_cache`` directly, so the cache's internal shape can evolve
        without breaking every callsite. See #171.
        """
        return self._meta_cache.get(node_id)

    def iter_cached_meta(self) -> Iterable[tuple[str, _CachedMeta]]:
        """Iterate over ``(node_id, cached_meta)`` pairs.

        Snapshots the view so the caller can iterate safely even if the
        underlying cache is mutated during iteration (rare, but possible
        via concurrent writes / file-watcher projections).
        """
        return list(self._meta_cache.items())

    @property
    def document_count(self) -> int:
        """Synchronous count of known documents (from in-memory cache)."""
        return len(self._meta_cache)

    @property
    def stale_document_count(self) -> int:
        """Synchronous count of documents whose expires_at is set and in the past."""
        now = datetime.now(UTC)
        count = 0
        for cached in self._meta_cache.values():
            if cached.expires_at is not None:
                exp = normalize_datetime(cached.expires_at)
                if exp < now:
                    count += 1
        return count

    def rescan(self) -> None:
        """Public wrapper around _scan_existing() for index rebuilds."""
        self._scan_existing()
