"""Knowledge module - Markdown document CRUD with frontmatter."""

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import frontmatter

from lithos.config import get_config
from lithos.telemetry import lithos_metrics, traced

logger = logging.getLogger(__name__)

# Wiki-link pattern: [[target]] or [[target|display]]
WIKI_LINK_PATTERN = re.compile(r"\[\[([^\]\[|]*[a-zA-Z][^\]\[|]*)(?:\|([^\]]+))?\]\]")


@dataclass
class WikiLink:
    """Represents a wiki-link in document content."""

    target: str
    display: str | None = None

    @property
    def display_text(self) -> str:
        """Get display text, defaulting to target."""
        return self.display or self.target


_KNOWN_METADATA_KEYS = frozenset(
    {
        "id",
        "title",
        "author",
        "created_at",
        "updated_at",
        "tags",
        "aliases",
        "confidence",
        "contributors",
        "source",
        "source_url",
        "supersedes",
    }
)


_TRACKING_PARAMS = frozenset(
    {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid"}
)

_DEFAULT_PORTS = {"https": 443, "http": 80}


def normalize_url(raw: str) -> str:
    """Canonicalize a URL for dedup comparison.

    Rules:
    - Lowercase scheme and host
    - Remove fragment
    - Remove default ports (:443 for https, :80 for http)
    - Strip trailing slash on non-root paths
    - Sort query params alphabetically
    - Remove tracking params (utm_*, fbclid)
    - Preserve ref param
    - Reject non-http/https schemes (raises ValueError)
    - Reject empty/whitespace-only input (raises ValueError)
    """
    if not raw or not raw.strip():
        raise ValueError("URL must not be empty or whitespace-only")

    parsed = urlparse(raw.strip())

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"Only http/https URLs are supported, got: {scheme!r}")

    host = parsed.hostname or ""
    host = host.lower()

    # Remove default port
    port = parsed.port
    if port and port == _DEFAULT_PORTS.get(scheme):
        port = None

    netloc = host
    if port:
        netloc = f"{host}:{port}"

    # Strip trailing slash on non-root paths
    path = parsed.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    # Sort query params, removing tracking params
    query_params = parse_qs(parsed.query, keep_blank_values=True)
    filtered = {k: v for k, v in sorted(query_params.items()) if k not in _TRACKING_PARAMS}
    query = urlencode(filtered, doseq=True)

    # No fragment
    return urlunparse((scheme, netloc, path, "", query, ""))


@dataclass
class KnowledgeMetadata:
    """Document metadata stored in YAML frontmatter."""

    id: str
    title: str
    author: str
    created_at: datetime
    updated_at: datetime
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    confidence: float = 1.0
    contributors: list[str] = field(default_factory=list)
    source: str | None = None
    source_url: str | None = None
    supersedes: str | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary for frontmatter.

        Unknown fields stored in ``extra`` are merged back so they
        survive read-write cycles (important for forward compatibility
        with extension plans that add new metadata fields).
        """
        result = {
            "id": self.id,
            "title": self.title,
            "author": self.author,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "tags": self.tags,
            "aliases": self.aliases,
            "confidence": self.confidence,
            "contributors": self.contributors,
            "source": self.source,
            "supersedes": self.supersedes,
        }
        if self.source_url is not None:
            result["source_url"] = self.source_url
        # Merge unknown fields — known keys always take precedence.
        for key, value in self.extra.items():
            if key not in result:
                result[key] = value
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "KnowledgeMetadata":
        """Create from dictionary.

        Keys not recognised as known metadata are captured in ``extra``
        so they are preserved through read-write cycles.
        """
        created_at = data.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        elif created_at is None:
            created_at = datetime.now(timezone.utc)

        updated_at = data.get("updated_at")
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at)
        elif updated_at is None:
            updated_at = datetime.now(timezone.utc)

        extra = {k: v for k, v in data.items() if k not in _KNOWN_METADATA_KEYS}

        return cls(
            id=data.get("id", str(uuid.uuid4())),
            title=data.get("title", "Untitled"),
            author=data.get("author", "unknown"),
            created_at=created_at,
            updated_at=updated_at,
            tags=data.get("tags", []),
            aliases=data.get("aliases", []),
            confidence=data.get("confidence", 1.0),
            contributors=data.get("contributors", []),
            source=data.get("source"),
            source_url=data.get("source_url"),
            supersedes=data.get("supersedes"),
            extra=extra,
        )


@dataclass
class KnowledgeDocument:
    """A knowledge document with content and metadata."""

    id: str
    title: str
    content: str
    metadata: KnowledgeMetadata
    path: Path
    links: list[WikiLink] = field(default_factory=list)

    @property
    def slug(self) -> str:
        """Get URL-safe slug from title."""
        return slugify(self.title)

    @property
    def full_content(self) -> str:
        """Get full content including title as H1."""
        return f"# {self.title}\n\n{self.content}"

    def to_markdown(self) -> str:
        """Convert to markdown string with frontmatter."""
        post = frontmatter.Post(self.full_content, **self.metadata.to_dict())
        return frontmatter.dumps(post)


@dataclass
class DuplicateInfo:
    """Information about a duplicate document."""

    id: str
    title: str
    source_url: str | None = None


@dataclass
class WriteResult:
    """Structured result type for create/update operations."""

    status: Literal["created", "updated", "duplicate", "error"]
    document: KnowledgeDocument | None = None
    warnings: list[str] = field(default_factory=list)
    error_code: str | None = None
    message: str | None = None
    duplicate_of: DuplicateInfo | None = None


def slugify(text: str) -> str:
    """Convert text to URL-safe slug."""
    # Convert to lowercase
    slug = text.lower()
    # Replace spaces and underscores with hyphens
    slug = re.sub(r"[\s_]+", "-", slug)
    # Remove non-alphanumeric characters except hyphens
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    # Collapse multiple hyphens
    slug = re.sub(r"-+", "-", slug)
    # Strip leading/trailing hyphens
    slug = slug.strip("-")
    return slug or "untitled"


def generate_slug(title: str) -> str:
    """Generate slug from title (alias for slugify)."""
    return slugify(title)


def parse_wiki_links(content: str) -> list[WikiLink]:
    """Extract wiki-links from content."""
    links = []
    for match in WIKI_LINK_PATTERN.finditer(content):
        target = match.group(1).strip()
        display = match.group(2)
        display = display.strip() if display else target
        links.append(WikiLink(target=target, display=display))
    return links


def extract_title_from_content(content: str) -> tuple[str, str]:
    """Extract title from H1 header if present.

    Returns:
        Tuple of (title, remaining_content)
    """
    lines = content.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            remaining = "\n".join(lines[i + 1 :]).strip()
            return title, remaining
        elif stripped and not stripped.startswith("#"):
            # Non-empty, non-header line found before H1
            break
    return "", content


def truncate_content(content: str, max_length: int) -> tuple[str, bool]:
    """Truncate content at paragraph or sentence boundary.

    Returns:
        Tuple of (truncated_content, was_truncated)
    """
    if len(content) <= max_length:
        return content, False

    # Reserve space for ellipsis
    effective_max = max_length - 3

    # Find last paragraph break before limit
    truncated = content[:effective_max]
    last_para = truncated.rfind("\n\n")
    if last_para > effective_max // 2:
        result = content[:last_para].strip()
        if len(result) <= max_length:
            return result, True

    # Find last sentence break
    last_sentence = max(
        truncated.rfind(". "),
        truncated.rfind("! "),
        truncated.rfind("? "),
    )
    if last_sentence > effective_max // 2:
        result = content[: last_sentence + 1].strip()
        if len(result) <= max_length:
            return result, True

    # Hard truncate at word boundary
    last_space = truncated.rfind(" ")
    if last_space > 0:
        return content[:last_space].strip() + "...", True

    return content[:effective_max] + "...", True


class _UnsetType:
    """Sentinel type for omit-vs-clear distinction on optional fields."""

    _instance: "_UnsetType | None" = None

    def __new__(cls) -> "_UnsetType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance


_UNSET = _UnsetType()
"""Sentinel for omit-vs-clear distinction on optional fields."""


class KnowledgeManager:
    """Manages knowledge documents - CRUD operations."""

    def __init__(self):
        """Initialize knowledge manager."""
        self.config = get_config()
        self.knowledge_path = self.config.storage.knowledge_path
        self._id_to_path: dict[str, Path] = {}
        self._slug_to_id: dict[str, str] = {}
        self._source_url_to_id: dict[str, str] = {}
        self._write_lock = asyncio.Lock()
        self.duplicate_url_count: int = 0
        self._scan_existing()

    def _scan_existing(self) -> None:
        """Scan existing documents and build indices."""
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

        for rel_path, md_file in candidates:
            try:
                post = frontmatter.load(str(md_file))
                doc_id: str | None = post.metadata.get("id")  # type: ignore[assignment]
                title: str = post.metadata.get("title", "")  # type: ignore[assignment]
                if doc_id:
                    self._id_to_path[doc_id] = rel_path
                    if title:
                        self._slug_to_id[slugify(title)] = doc_id

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
            except Exception:
                pass  # Skip invalid files

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
    async def create(
        self,
        title: str,
        content: str,
        agent: str,
        tags: list[str] | None = None,
        confidence: float = 1.0,
        path: str | None = None,
        source: str | None = None,
        source_url: str | None = None,
    ) -> WriteResult:
        """Create a new knowledge document.

        Returns WriteResult with status 'created', 'duplicate', or 'error'.
        """
        async with self._write_lock:
            lithos_metrics.knowledge_ops.add(1, {"op": "create"})

            # Validate and normalize source_url
            norm_url: str | None = None
            if source_url is not None:
                try:
                    norm_url = normalize_url(source_url)
                except ValueError as e:
                    return WriteResult(
                        status="error",
                        error_code="invalid_input",
                        message=str(e),
                    )

                # Check dedup map
                existing_id = self._source_url_to_id.get(norm_url)
                if existing_id is not None:
                    try:
                        existing_doc, _ = await self.read(id=existing_id)
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

            doc_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc)

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
            )

            # Determine file path
            slug = slugify(title)
            file_path = Path(path) / f"{slug}.md" if path else Path(f"{slug}.md")
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

            # Write to disk
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(doc.to_markdown())

            # Update indices
            self._id_to_path[doc_id] = file_path
            self._slug_to_id[slug] = doc_id
            if norm_url is not None:
                self._source_url_to_id[norm_url] = doc_id

            return WriteResult(status="created", document=doc)

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

        post = frontmatter.load(str(full_path))
        metadata = KnowledgeMetadata.from_dict(post.metadata)

        # Extract title and content from body
        title, content = extract_title_from_content(post.content)
        if not title:
            title = metadata.title

        # Parse wiki-links
        links = parse_wiki_links(content)

        # Truncate if requested
        truncated = False
        if max_length:
            content, truncated = truncate_content(content, max_length)

        doc = KnowledgeDocument(
            id=metadata.id,
            title=title,
            content=content,
            metadata=metadata,
            path=file_path,
            links=links,
        )

        return doc, truncated

    @traced("lithos.knowledge.update")
    async def update(
        self,
        id: str,
        agent: str,
        content: str | None = None,
        title: str | None = None,
        tags: list[str] | None = None,
        confidence: float | None = None,
        source_url: str | None | _UnsetType = _UNSET,
    ) -> WriteResult:
        """Update an existing document.

        source_url semantics:
        - _UNSET (default): preserve existing source_url, no map change
        - None: clear existing source_url, remove from map
        - str: normalize, allow if same doc owns it, reject if different doc owns it
        """
        async with self._write_lock:
            lithos_metrics.knowledge_ops.add(1, {"op": "update"})
            doc, _ = await self.read(id=id)
            old_slug = slugify(doc.metadata.title)
            old_source_url = doc.metadata.source_url

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
                            status="error",
                            error_code="invalid_input",
                            message=str(e),
                        )

                    existing_owner = self._source_url_to_id.get(new_norm)
                    if existing_owner is not None and existing_owner != id:
                        try:
                            existing_doc, _ = await self.read(id=existing_owner)
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

            # Update fields
            if content is not None:
                doc.content = content
                doc.links = parse_wiki_links(content)
            if title is not None:
                doc.title = title
                doc.metadata.title = title
            if tags is not None:
                doc.metadata.tags = tags
            if confidence is not None:
                doc.metadata.confidence = confidence

            # Update metadata
            doc.metadata.updated_at = datetime.now(timezone.utc)
            if agent not in doc.metadata.contributors and agent != doc.metadata.author:
                doc.metadata.contributors.append(agent)

            # Write to disk
            _safe_path, full_path = self._resolve_safe_path(doc.path)
            full_path.write_text(doc.to_markdown())

            # Keep slug index in sync when title changes.
            new_slug = slugify(doc.metadata.title)
            if new_slug != old_slug:
                if self._slug_to_id.get(old_slug) == id:
                    del self._slug_to_id[old_slug]
                self._slug_to_id[new_slug] = id

            return WriteResult(status="updated", document=doc)

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
            del self._id_to_path[id]
            # Remove from slug index
            self._slug_to_id = {k: v for k, v in self._slug_to_id.items() if v != id}

            return True, str(file_path)

    async def list_all(
        self,
        path_prefix: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        tags: list[str] | None = None,
        author: str | None = None,
    ) -> tuple[list[KnowledgeDocument], int]:
        """List all documents with optional filtering."""
        docs = []
        total = 0
        normalized_since = _normalize_datetime(since) if since else None

        for doc_id in self._id_to_path:
            try:
                doc, _ = await self.read(id=doc_id)

                # Apply filters
                if path_prefix and not str(doc.path).startswith(path_prefix):
                    continue
                if tags and not any(t in doc.metadata.tags for t in tags):
                    continue
                if author and doc.metadata.author != author:
                    continue
                if normalized_since:
                    doc_updated = _normalize_datetime(doc.metadata.updated_at)
                    if doc_updated < normalized_since:
                        continue

                total += 1
                if total > offset and len(docs) < limit:
                    docs.append(doc)
            except Exception:
                pass

        return docs, total

    async def get_all_tags(self) -> dict[str, int]:
        """Get all tags with document counts."""
        tag_counts: dict[str, int] = {}

        for doc_id in self._id_to_path:
            try:
                doc, _ = await self.read(id=doc_id)
                for tag in doc.metadata.tags:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
            except Exception:
                pass

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

    def get_id_by_slug(self, slug: str) -> str | None:
        """Get document ID by slug."""
        return self._slug_to_id.get(slug)

    def get_id_by_path(self, path: str | Path) -> str | None:
        """Get document ID by relative/absolute path."""
        candidate = Path(path)

        if candidate.is_absolute():
            try:
                candidate = candidate.resolve().relative_to(self.knowledge_path.resolve())
            except ValueError:
                return None

        if not candidate.suffix:
            candidate = candidate.with_suffix(".md")

        for doc_id, doc_path in self._id_to_path.items():
            if doc_path == candidate:
                return doc_id
        return None

    def get_all_slugs(self) -> dict[str, str]:
        """Get mapping of all slugs to IDs."""
        return dict(self._slug_to_id)


def _normalize_datetime(dt: datetime) -> datetime:
    """Normalize datetime values for safe comparison."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
