"""Knowledge module - Markdown document CRUD with frontmatter."""

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from exogram.config import get_config

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
    supersedes: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for frontmatter."""
        return {
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

    @classmethod
    def from_dict(cls, data: dict) -> "KnowledgeMetadata":
        """Create from dictionary."""
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
            supersedes=data.get("supersedes"),
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


class KnowledgeManager:
    """Manages knowledge documents - CRUD operations."""

    def __init__(self):
        """Initialize knowledge manager."""
        self.config = get_config()
        self.knowledge_path = self.config.storage.knowledge_path
        self._id_to_path: dict[str, Path] = {}
        self._slug_to_id: dict[str, str] = {}
        self._scan_existing()

    def _scan_existing(self) -> None:
        """Scan existing documents and build indices."""
        if not self.knowledge_path.exists():
            return

        for md_file in self.knowledge_path.rglob("*.md"):
            try:
                post = frontmatter.load(md_file)
                doc_id = post.metadata.get("id")
                title = post.metadata.get("title", "")
                if doc_id:
                    rel_path = md_file.relative_to(self.knowledge_path)
                    self._id_to_path[doc_id] = rel_path
                    if title:
                        self._slug_to_id[slugify(title)] = doc_id
            except Exception:
                pass  # Skip invalid files

    async def create(
        self,
        title: str,
        content: str,
        agent: str,
        tags: list[str] | None = None,
        confidence: float = 1.0,
        path: str | None = None,
        source: str | None = None,
    ) -> KnowledgeDocument:
        """Create a new knowledge document."""
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
        )

        # Determine file path
        slug = slugify(title)
        file_path = Path(path) / f"{slug}.md" if path else Path(f"{slug}.md")

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
        full_path = self.knowledge_path / file_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(doc.to_markdown())

        # Update indices
        self._id_to_path[doc_id] = file_path
        self._slug_to_id[slug] = doc_id

        return doc

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

        full_path = self.knowledge_path / file_path
        if not full_path.exists():
            raise FileNotFoundError(f"Document not found: {file_path}")

        post = frontmatter.load(full_path)
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

    async def update(
        self,
        id: str,
        agent: str,
        content: str | None = None,
        title: str | None = None,
        tags: list[str] | None = None,
        confidence: float | None = None,
    ) -> KnowledgeDocument:
        """Update an existing document."""
        doc, _ = await self.read(id=id)

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
        full_path = self.knowledge_path / doc.path
        full_path.write_text(doc.to_markdown())

        return doc

    async def delete(self, id: str) -> bool:
        """Delete a document."""
        if id not in self._id_to_path:
            return False

        file_path = self._id_to_path[id]
        full_path = self.knowledge_path / file_path

        if full_path.exists():
            full_path.unlink()

        # Update indices
        del self._id_to_path[id]
        # Remove from slug index
        self._slug_to_id = {k: v for k, v in self._slug_to_id.items() if v != id}

        return True

    async def list_all(
        self,
        limit: int = 100,
        offset: int = 0,
        tags: list[str] | None = None,
        author: str | None = None,
    ) -> tuple[list[KnowledgeDocument], int]:
        """List all documents with optional filtering."""
        docs = []
        total = 0

        for doc_id in self._id_to_path:
            try:
                doc, _ = await self.read(id=doc_id)

                # Apply filters
                if tags and not any(t in doc.metadata.tags for t in tags):
                    continue
                if author and doc.metadata.author != author:
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

    def get_id_by_slug(self, slug: str) -> str | None:
        """Get document ID by slug."""
        return self._slug_to_id.get(slug)

    def get_all_slugs(self) -> dict[str, str]:
        """Get mapping of all slugs to IDs."""
        return dict(self._slug_to_id)
