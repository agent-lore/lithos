"""Knowledge module - Markdown document CRUD with frontmatter.

The corpus *file format* lives next door in :mod:`lithos.frontmatter_codec`;
this module owns the corpus *store* — CRUD, disk I/O, slug/path allocation, and
the reconcile seam that rebuilds the derived views (ADR-0001). It reads and
writes bytes; the codec turns those bytes into documents and back.

The derived *query view* — the metadata cache, its inverted indexes, and the
in-memory provenance maps — is delegated to :mod:`lithos.corpus_index`, held as
``self._index`` and rebuilt from the corpus scan on construction.
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
from lithos.corpus_index import CachedMeta, CorpusIndex, ScannedNote
from lithos.errors import CorpusScanError, SlugCollisionError
from lithos.frontmatter_codec import (
    KnowledgeDocument,
    KnowledgeMetadata,
    decode,
    encode,
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
        self._write_lock = asyncio.Lock()
        # The derived, in-memory query view over the Corpus (metadata cache,
        # inverted indexes, path/slug/url maps, provenance graph). The manager
        # owns files and policy; the index owns query acceleration.
        self._index = CorpusIndex()
        self._scan_existing()

    @property
    def duplicate_url_count(self) -> int:
        """Number of duplicate source_urls skipped on the last scan."""
        return self._index.duplicate_url_count

    def _scan_existing(self) -> None:
        """Scan the Corpus from disk and rebuild the derived index.

        The manager owns the file walk — which files, in what order, and
        skipping any that won't parse — and hands the frontmatter it read to
        :meth:`CorpusIndex.rebuild`, which owns the projection. Candidates are
        walked in sorted order so the index's first-seen-wins tie-breaks
        (slug and source-url collisions) are deterministic.
        """
        scanned: list[ScannedNote] = []
        if self.knowledge_path.exists():
            base_path = self.knowledge_path.resolve()
            candidates: list[tuple[Path, Path]] = []
            for md_file in self.knowledge_path.rglob("*.md"):
                resolved = md_file.resolve()
                if not resolved.is_relative_to(base_path):
                    continue
                candidates.append((md_file.relative_to(self.knowledge_path), md_file))
            candidates.sort(key=lambda t: t[0])
            for rel_path, md_file in candidates:
                try:
                    post = frontmatter.load(str(md_file))
                    doc_id: str | None = post.metadata.get("id")  # type: ignore[assignment]
                    if doc_id:
                        title = post.metadata.get("title", "")
                        scanned.append(
                            ScannedNote(
                                doc_id=doc_id,
                                title=title if isinstance(title, str) else "",
                                frontmatter=dict(post.metadata),
                                rel_path=rel_path,
                            )
                        )
                except Exception as e:
                    logger.warning("Skipping invalid file %s: %s", md_file, e)
        self._index.rebuild(scanned)

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
                existing_id = self._index.id_by_source_url(norm_url)
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
                        self._index.remove_source_url(norm_url, existing_id)

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
            existing_slug_id = self._index.id_by_slug(slug)
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
            existing_path_id = self._index.id_by_relpath(file_path)
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

            # Register across every derived index (id/path/slug/url maps,
            # provenance graph, metadata cache). Warnings name any source that
            # does not yet exist; the note still records the dangling reference.
            cached = CachedMeta.from_metadata(metadata, file_path, seq=self._index.next_seq())
            warnings = self._index.add_document(
                doc_id,
                cached,
                title=title,
                norm_url=norm_url,
                sources=normalized_provenance,
            )

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
            file_path = self._index.relpath_of(id)
            if file_path is None:
                raise FileNotFoundError(f"Document not found: {id}")
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
        indexed_sources = self._index.doc_sources(doc.metadata.id)
        if indexed_sources:
            doc.metadata.derived_from_ids = indexed_sources

        return doc, truncated

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
                    existing_owner = self._index.id_by_slug(new_slug)
                    if existing_owner is not None and existing_owner != id:
                        raise SlugCollisionError(new_slug, existing_owner)

            # Handle source_url update
            if not isinstance(source_url, _UnsetType):
                if source_url is None:
                    # Clear source_url
                    if old_source_url:
                        try:
                            old_norm = normalize_url(old_source_url)
                            self._index.remove_source_url(old_norm, id)
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

                    existing_owner = self._index.id_by_source_url(new_norm)
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
                            self._index.remove_source_url(new_norm, existing_owner)

                    # Remove old mapping if URL changed
                    if old_source_url:
                        try:
                            old_norm = normalize_url(old_source_url)
                            if old_norm != new_norm:
                                self._index.remove_source_url(old_norm, id)
                        except ValueError:
                            pass

                    doc.metadata.source_url = new_norm
                    self._index.set_source_url(new_norm, id)

            # Handle derived_from_ids update
            warnings: list[str] = []
            if not isinstance(derived_from_ids, _UnsetType):
                if derived_from_ids is None or derived_from_ids == []:
                    # Clear provenance
                    doc.metadata.derived_from_ids = []
                    self._index.replace_provenance(id, [])
                else:
                    # Replace with new list — validate first
                    try:
                        normalized = validate_derived_from_ids(derived_from_ids, self_id=id)
                    except ValueError as e:
                        return WriteResult(
                            status="invalid_input",
                            message=str(e),
                        )

                    doc.metadata.derived_from_ids = normalized
                    warnings = self._index.replace_provenance(id, normalized)

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
                self._index.reroute_slug(old_slug, new_slug, id)

            # Update _id_to_title if title changed
            if title is not None:
                self._index.set_title(id, title)

            # Update metadata cache + inverted index. Preserve the insertion
            # ordinal so list ordering/pagination is unchanged by an update.
            old_cached = self._index.get_cached_meta(id)
            cached = CachedMeta.from_metadata(
                doc.metadata,
                doc.path,
                seq=old_cached.seq if old_cached is not None else self._index.next_seq(),
            )
            self._index.reindex_document(id, cached)

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
            if not self._index.has_document(id):
                return False, ""

            # Read doc to get source_url before deleting
            try:
                doc, _ = await self.read(id=id)
                if doc.metadata.source_url:
                    try:
                        norm = normalize_url(doc.metadata.source_url)
                        self._index.remove_source_url(norm, id)
                    except ValueError:
                        pass
            except FileNotFoundError:
                pass

            file_path = self._index.relpath_of(id)
            assert file_path is not None  # has_document guaranteed it above
            _safe_path, full_path = self._resolve_safe_path(file_path)

            if full_path.exists():
                full_path.unlink()

            # Drop the document from every derived index (maps, provenance graph
            # — re-orphaning anything that derived from it — and metadata cache).
            self._index.remove_document(id)

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
        candidate_ids = self._index.candidate_ids(
            tags=tags,
            author=author,
            metadata_match=metadata_match,
            exclude_status=exclude_status,
            entities=entities,
        )

        if candidate_ids is None:
            # No equality filter — full scan (existing behaviour + ordering).
            matching_ids: list[str] = []
            for doc_id, cached in self._index.iter_cached_meta():
                if exclude_status and cached.status in exclude_status:
                    continue
                if path_prefix and not str(cached.path).startswith(path_prefix):
                    continue
                if normalized_since and normalize_datetime(cached.updated_at) < normalized_since:
                    continue
                matching_ids.append(doc_id)
        else:
            # Index path — refine the (small) candidate set, then restore the
            # cache insertion order via the stored seq for stable paging.
            refined: list[CachedMeta] = []
            refined_ids: list[str] = []
            for doc_id in candidate_ids:
                cached = self._index.get_cached_meta(doc_id)
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
        return self._index.metadata_candidate_ids(metadata_match)

    def entities_candidate_ids(self, entities: list[str] | None) -> set[str] | None:
        """Public wrapper: candidate ids for an ``entities`` filter (#316).

        Returns ``None`` when ``entities`` is empty/None (no filtering),
        otherwise the set of doc ids carrying every named entity (sub-linear,
        index-based). Used by ``lithos_search`` to post-filter hits without
        scanning the cache.
        """
        return self._index.entities_candidate_ids(entities)

    async def get_all_tags(self) -> dict[str, int]:
        """Get all tags with document counts (from in-memory cache)."""
        return self._index.all_tags()

    async def find_by_source_url(self, url: str) -> KnowledgeDocument | None:
        """Look up a document by source URL (internal only, not MCP-exposed).

        Normalizes the input URL before lookup. Does not acquire _write_lock
        (read-only on the map).
        """
        try:
            norm = normalize_url(url)
        except ValueError:
            return None

        doc_id = self._index.id_by_source_url(norm)
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
        is_new = not self._index.has_document(doc_id)

        # Update the path and slug maps (the on-disk path/title may have moved).
        self._index.set_path(doc_id, file_path)
        self._index.reroute_slug_for(doc_id, slugify(title))

        # Update source_url index (first-owner-wins; clear this doc's old urls).
        raw_url = metadata.source_url
        if raw_url:
            try:
                norm = normalize_url(raw_url)
                self._index.clear_source_urls_for(doc_id)
                existing_owner = self._index.id_by_source_url(norm)
                if existing_owner is not None and existing_owner != doc_id:
                    logger.warning(
                        "source_url collision in sync_from_disk: %s owned by %s, "
                        "skipping assignment for %s",
                        norm,
                        existing_owner,
                        doc_id,
                    )
                else:
                    self._index.set_source_url(norm, doc_id)
            except ValueError:
                pass
        else:
            self._index.clear_source_urls_for(doc_id)

        self._index.set_title(doc_id, title)

        # Update provenance (diff for a modified note; classify + auto-resolve
        # for a newly-seen one). Sync is silent — no missing-source warnings.
        new_sources = normalize_derived_from_ids_lenient(
            metadata.derived_from_ids or [], self_id=doc_id
        )
        self._index.sync_provenance(doc_id, new_sources, is_new=is_new)

        # Update metadata cache + inverted index, preserving the insertion
        # ordinal so list ordering stays stable.
        old_cached = self._index.get_cached_meta(doc_id)
        cached = CachedMeta.from_metadata(
            metadata,
            file_path,
            seq=old_cached.seq if old_cached is not None else self._index.next_seq(),
        )
        self._index.reindex_document(doc_id, cached)

        return doc

    # ==================== Derived-index read accessors ====================
    # These delegate to the CorpusIndex so callers keep a stable manager-level
    # API while the projection owns the state (issue #171/#264).

    def get_id_by_slug(self, slug: str) -> str | None:
        """Get document ID by slug."""
        return self._index.id_by_slug(slug)

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

        return self._index.id_by_relpath(candidate)

    def get_all_slugs(self) -> dict[str, str]:
        """Get mapping of all slugs to IDs."""
        return self._index.all_slugs()

    # ==================== Public Provenance Accessors ====================

    def get_doc_sources(self, doc_id: str) -> list[str]:
        """Get the source IDs this document derives from."""
        return self._index.doc_sources(doc_id)

    def iter_doc_sources(self) -> Iterable[tuple[str, list[str]]]:
        """Iterate over ``(doc_id, source_ids)`` pairs for every known document.

        Snapshots the underlying index so callers can iterate safely even if the
        cache is mutated concurrently; returned lists are fresh copies. The
        public bulk-read counterpart to :meth:`get_doc_sources` (issue #264).
        """
        return self._index.iter_doc_sources()

    def get_derived_docs(self, doc_id: str) -> set[str]:
        """Get IDs of documents derived from this document."""
        return self._index.derived_docs(doc_id)

    def get_unresolved_sources(self, doc_id: str) -> list[str]:
        """Get unresolved source IDs for a document."""
        return self._index.unresolved_sources(doc_id)

    def provenance_neighbours(
        self, start_id: str, direction: Literal["sources", "derived"], depth: int
    ) -> list[dict[str, str]]:
        """BFS the provenance ``derived_from`` maps; see :meth:`CorpusIndex.provenance_neighbours`."""
        return self._index.provenance_neighbours(start_id, direction, depth)

    def get_title_by_id(self, doc_id: str) -> str:
        """Get document title by ID, returning empty string if unknown."""
        return self._index.title_by_id(doc_id)

    def has_document(self, doc_id: str) -> bool:
        """Check whether a document ID exists."""
        return self._index.has_document(doc_id)

    def iter_doc_ids(self) -> Iterable[str]:
        """Snapshot every known document ID (for prefix/UUID resolution)."""
        return self._index.iter_doc_ids()

    def iter_source_urls(self) -> Iterable[tuple[str, str]]:
        """Snapshot ``(normalized_source_url, doc_id)`` pairs."""
        return self._index.iter_source_urls()

    def get_cached_meta(self, node_id: str) -> CachedMeta | None:
        """Return cached metadata for a node, or ``None`` if unknown.

        The public read accessor for the derived metadata cache. Callers outside
        ``KnowledgeManager`` — the LCMA scout, rerank, reinforcement, and enrich
        paths, plus the MCP server's feedback and node_stats handlers — should
        use this rather than the index directly (#171).
        """
        return self._index.get_cached_meta(node_id)

    def iter_cached_meta(self) -> Iterable[tuple[str, CachedMeta]]:
        """Iterate over ``(node_id, cached_meta)`` pairs (snapshot for safe iteration)."""
        return self._index.iter_cached_meta()

    @property
    def document_count(self) -> int:
        """Synchronous count of known documents (from in-memory cache)."""
        return self._index.document_count

    @property
    def stale_document_count(self) -> int:
        """Synchronous count of documents whose expires_at is set and in the past."""
        return self._index.stale_document_count

    def rescan(self) -> None:
        """Public wrapper around _scan_existing() for index rebuilds."""
        self._scan_existing()
