"""The Corpus file format: Markdown + YAML frontmatter <-> KnowledgeDocument.

:func:`decode` and :func:`encode` are the format's two ends. The rest of this
module is the vocabulary they are defined in — the record types, and the
validators and normalisers the write boundary applies before a note reaches
disk. Those rules are not incidental: unknown-key preservation, timezone
normalisation, confidence clamping and LCMA read-time defaults are each what
keeps a note readable by a Lithos that did not write it.

It lives apart from :mod:`lithos.knowledge` for two reasons. **Locality**: the
format has more than one reader, and kept inside the manager every
``frontmatter.load`` site had to re-remember the rules — the two decode paths
had already drifted into near-duplicates. **Direction**: the codec knows nothing
of storage, search, graphs or agents, depending on nothing but the stdlib and
``frontmatter``, which is what lets :mod:`lithos.graph` and
:mod:`lithos.provenance` name the document *type* without importing the
*manager* — the dependency that tangled them into an import cycle with it. It
belongs to the Knowledge component (it is the corpus format) but is held to
Foundation discipline by import-linter, as :mod:`lithos._merge` is.

Nothing here touches a disk: callers pass bytes they have already read, so every
rule below is testable without a manager or a filesystem.
"""

from __future__ import annotations

import json
import logging
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import frontmatter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Frontmatter field vocabulary
# ---------------------------------------------------------------------------

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
        "derived_from_ids",
        "expires_at",
        "version",
        # LCMA fields
        "schema_version",
        "namespace",
        "access_scope",
        "note_type",
        "status",
        "summaries",
        "entities",
        "entities_extractor",
    }
)

# Valid LCMA enum values
VALID_ACCESS_SCOPES = frozenset({"shared", "task", "agent_private"})
VALID_NOTE_TYPES = frozenset(
    {"observation", "agent_finding", "summary", "concept", "task_record", "hypothesis"}
)
VALID_STATUSES = frozenset({"active", "archived", "quarantined"})

# Wiki-link pattern: [[target]] or [[target|display]]
WIKI_LINK_PATTERN = re.compile(r"\[\[([^\]\[|]*[a-zA-Z][^\]\[|]*)(?:\|([^\]]+))?\]\]")


def normalize_datetime(dt: datetime) -> datetime:
    """Return *dt* in UTC, treating a naive value as already-UTC.

    Frontmatter is hand-editable, so a note can carry either form; every
    comparison must go through here or it risks raising on mixed awareness.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# ---------------------------------------------------------------------------
# Read-side healing — never raises; a bad note must still load
# ---------------------------------------------------------------------------


def _parse_version(value: object) -> int:
    """Parse a version value from frontmatter, falling back to 1 on bad input."""
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning(
            "_parse_version: non-numeric version value %r in frontmatter; defaulting to 1",
            value,
        )
        return 1
    if parsed < 1:
        logger.warning(
            "_parse_version: non-positive version value %r in frontmatter; clamping to 1",
            parsed,
        )
        return 1
    return parsed


def _parse_confidence(value: object) -> float:
    """Parse a confidence value from frontmatter, falling back to 1.0 on bad input (#312).

    ``dict.get("confidence", 1.0)`` only applies the default when the key is
    absent; a key present with ``null`` (or a string) would otherwise load as-is
    and crash numeric comparisons downstream (e.g. ``cache_lookup``).
    """
    # bool is an int subclass: float(False) would silently become 0.0 and
    # filter the doc out of every lookup, so treat it as non-numeric noise.
    if isinstance(value, bool):
        logger.warning(
            "_parse_confidence: non-numeric confidence value %r in frontmatter; defaulting to 1.0",
            value,
        )
        return 1.0
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning(
            "_parse_confidence: non-numeric confidence value %r in frontmatter; defaulting to 1.0",
            value,
        )
        return 1.0
    if math.isnan(parsed) or math.isinf(parsed):
        logger.warning(
            "_parse_confidence: non-finite confidence value %r in frontmatter; defaulting to 1.0",
            value,
        )
        return 1.0
    if not 0.0 <= parsed <= 1.0:
        clamped = min(max(parsed, 0.0), 1.0)
        logger.warning(
            "_parse_confidence: out-of-range confidence value %r in frontmatter; clamping to %s",
            parsed,
            clamped,
        )
        return clamped
    return parsed


def normalize_derived_from_ids_lenient(ids: list[str], self_id: str | None = None) -> list[str]:
    """Normalize derived_from_ids leniently for disk ingestion.

    Like validate_derived_from_ids() but logs warnings and skips
    invalid entries instead of raising ValueError.
    Returns a deduplicated, sorted list of lowercased UUID strings.
    """
    normalized: list[str] = []
    for raw in ids:
        if not isinstance(raw, str):
            logger.warning("Skipping non-string derived_from_ids entry: %r", raw)
            continue
        trimmed = raw.strip()
        if not trimmed:
            logger.warning("Skipping empty derived_from_ids entry")
            continue
        try:
            parsed = uuid.UUID(trimmed)
        except ValueError:
            logger.warning("Skipping invalid UUID in derived_from_ids: %r", trimmed)
            continue
        normalized.append(str(parsed))

    result = sorted(set(normalized))

    if self_id is not None:
        try:
            self_normalized = str(uuid.UUID(self_id))
            if self_normalized in result:
                logger.warning("Removing self-reference from derived_from_ids: %s", self_normalized)
                result.remove(self_normalized)
        except ValueError:
            pass

    return result


# ---------------------------------------------------------------------------
# Write-side validation — raises; a bad write must not reach disk
# ---------------------------------------------------------------------------


def validate_extra_metadata(extra: dict) -> None:
    """Validate free-form document metadata before it is stored in ``extra`` (#305).

    The ``extra`` dict serializes as top-level frontmatter keys, and
    ``KnowledgeMetadata.to_dict`` lets known keys win on collision. A key that
    shadows a reserved field would therefore be silently dropped on the next
    write. Enforcing this in the storage layer keeps the invariant true for
    every caller, not just the MCP boundary.

    Raises:
        ValueError: if ``extra`` is not a dict, has non-string keys, or uses a
            key reserved by known frontmatter fields.
    """
    if not isinstance(extra, dict):
        raise ValueError("metadata must be an object of string keys.")
    if any(not isinstance(k, str) for k in extra):
        raise ValueError("metadata keys must be strings.")
    reserved = sorted(set(extra) & _KNOWN_METADATA_KEYS)
    if reserved:
        raise ValueError(
            f"metadata keys collide with reserved frontmatter fields: {reserved}. "
            "Choose different keys."
        )


def validate_confidence(value: object) -> float:
    """Validate a confidence value at the write boundary (#312).

    Stricter than the read-side ``_parse_confidence``: writes fail fast with
    ``ValueError`` instead of healing, so ``null``/string values can never be
    persisted again. Integers are accepted and coerced (integer JSON over the
    MCP wire); bool is rejected because it is an ``int`` subclass, not a score.

    Raises:
        ValueError: if ``value`` is not a finite number in [0.0, 1.0].
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"confidence must be a number between 0.0 and 1.0, got {value!r}")
    parsed = float(value)
    if math.isnan(parsed) or math.isinf(parsed) or not 0.0 <= parsed <= 1.0:
        raise ValueError(f"confidence must be a number between 0.0 and 1.0, got {value!r}")
    return parsed


def validate_derived_from_ids(ids: list[str], self_id: str | None = None) -> list[str]:
    """Validate and normalize a list of derived-from document IDs.

    Returns a deduplicated, sorted list of lowercased UUID strings.
    Raises ValueError for invalid entries or self-references.
    """
    normalized: list[str] = []
    for raw in ids:
        if not isinstance(raw, str):
            raise ValueError(f"derived_from_ids entry must be a string, got {type(raw).__name__}")
        trimmed = raw.strip()
        if not trimmed:
            raise ValueError("derived_from_ids entry must not be empty or whitespace-only")
        try:
            parsed = uuid.UUID(trimmed)
        except ValueError as err:
            raise ValueError(f"Invalid UUID in derived_from_ids: {trimmed!r}") from err
        normalized.append(str(parsed))

    result = sorted(set(normalized))

    if self_id is not None:
        self_normalized = str(uuid.UUID(self_id))
        if self_normalized in result:
            raise ValueError(f"derived_from_ids must not contain self-reference: {self_normalized}")

    return result


# ---------------------------------------------------------------------------
# Metadata query helpers (#306)
# ---------------------------------------------------------------------------

# Scalar JSON types accepted as metadata_match *query* values (#306).
_METADATA_MATCH_SCALARS = (str, int, float, bool)


def validate_metadata_match(metadata_match: dict) -> None:
    """Validate a ``metadata_match`` filter (#306).

    Used by both ``lithos_list`` and ``lithos_task_list`` so the two surfaces
    share one ``invalid_input`` contract. Query values must be JSON scalars;
    a stored value that is a *list* is matched element-wise downstream, but the
    query value itself stays scalar (list/dict/null query values are rejected).

    Raises:
        ValueError: if ``metadata_match`` is not a dict, has a non-string or
            empty key, or a non-scalar value.
    """
    if not isinstance(metadata_match, dict):
        raise ValueError("metadata_match must be an object of string keys.")
    for key, value in metadata_match.items():
        if not isinstance(key, str) or not key:
            raise ValueError("metadata_match keys must be non-empty strings.")
        # bool is a subclass of int — both are allowed scalars; reject only
        # null, lists and dicts.
        if not isinstance(value, _METADATA_MATCH_SCALARS):
            raise ValueError(f"metadata_match[{key!r}] must be a string, number, or boolean.")


def extract_extra(frontmatter_meta: dict) -> dict:
    """Return the free-form metadata: keys not recognised as known fields.

    Single source of truth for "what counts as ``extra``", shared by
    ``KnowledgeMetadata.from_dict`` and the startup scan.
    """
    return {k: v for k, v in frontmatter_meta.items() if k not in _KNOWN_METADATA_KEYS}


def canonical_metadata_value(value: object) -> str:
    """Canonical, hashable bucket key for a metadata value (#306).

    JSON-equal values map to the same string, so equality matching is correct
    across types and across dict key orderings.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# URL canonicalisation
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Body helpers — the inverse pair of KnowledgeDocument.full_content
# ---------------------------------------------------------------------------


@dataclass
class WikiLink:
    """Represents a wiki-link in document content."""

    target: str
    display: str | None = None

    @property
    def display_text(self) -> str:
        """Get display text, defaulting to target."""
        return self.display or self.target


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

    The read-side inverse of ``KnowledgeDocument.full_content``, which
    re-attaches the H1 on write.

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
    result = slug or "untitled"
    logger.debug("slugify: title=%r slug=%r", text, result)
    return result


def derive_namespace(relative_path: Path) -> str:
    """Derive namespace from a note's path relative to knowledge_path.

    Subdirectory components are joined by ``/``.  Files directly under the
    knowledge root return ``"default"``.
    """
    parts = relative_path.parent.parts
    if not parts or parts == (".",):
        return "default"
    return "/".join(parts)


# ---------------------------------------------------------------------------
# The record types
# ---------------------------------------------------------------------------


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
    derived_from_ids: list[str] = field(default_factory=list)
    expires_at: datetime | None = None
    extra: dict = field(default_factory=dict)
    version: int = 1
    # LCMA fields — optional, defaults applied at read time by callers with path context
    schema_version: int | None = None
    namespace: str | None = None
    access_scope: str | None = None  # shared | task | agent_private
    note_type: str | None = (
        None  # observation | agent_finding | summary | concept | task_record | hypothesis
    )
    status: str | None = None  # active | archived | quarantined
    summaries: dict | None = None  # {short: str, long: str}
    entities: list[str] = field(default_factory=list)
    # Extractor version that wrote ``entities``; None means agent-curated
    # (never auto-overwritten). See lithos.lcma.entities (#313).
    entities_extractor: int | None = None

    @property
    def is_stale(self) -> bool:
        """Return True when expires_at is set and in the past (UTC)."""
        if self.expires_at is None:
            return False
        return datetime.now(UTC) > normalize_datetime(self.expires_at)

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
        result["version"] = self.version
        if self.source_url is not None:
            result["source_url"] = self.source_url
        if self.expires_at is not None:
            result["expires_at"] = self.expires_at.isoformat()
        if self.derived_from_ids:
            result["derived_from_ids"] = self.derived_from_ids
        # LCMA fields — only include when explicitly set
        if self.schema_version is not None:
            result["schema_version"] = self.schema_version
        if self.namespace is not None:
            result["namespace"] = self.namespace
        if self.access_scope is not None:
            result["access_scope"] = self.access_scope
        if self.note_type is not None:
            result["note_type"] = self.note_type
        if self.status is not None:
            result["status"] = self.status
        if self.summaries is not None:
            result["summaries"] = self.summaries
        if self.entities:
            result["entities"] = self.entities
        if self.entities_extractor is not None:
            result["entities_extractor"] = self.entities_extractor
        # Merge unknown fields — known keys always take precedence.
        for key, value in self.extra.items():
            if key not in result:
                result[key] = value
        return result

    @classmethod
    def from_dict(cls, data: dict) -> KnowledgeMetadata:
        """Create from dictionary.

        Keys not recognised as known metadata are captured in ``extra``
        so they are preserved through read-write cycles.
        """
        created_at = data.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        elif created_at is None:
            created_at = datetime.now(UTC)

        updated_at = data.get("updated_at")
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at)
        elif updated_at is None:
            updated_at = datetime.now(UTC)

        expires_at = data.get("expires_at")
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            else:
                expires_at = expires_at.astimezone(UTC)
        elif not isinstance(expires_at, datetime):
            expires_at = None

        extra = extract_extra(data)

        # Parse LCMA fields — only unpack what is present; defaults applied by caller
        schema_version_raw = data.get("schema_version")
        schema_version: int | None = None
        if schema_version_raw is not None:
            try:
                schema_version = int(schema_version_raw)
            except (TypeError, ValueError):
                schema_version = None

        summaries_raw = data.get("summaries")
        summaries: dict | None = None
        if isinstance(summaries_raw, dict):
            summaries = summaries_raw

        entities_extractor_raw = data.get("entities_extractor")
        entities_extractor: int | None = None
        if entities_extractor_raw is not None and not isinstance(entities_extractor_raw, bool):
            try:
                entities_extractor = int(entities_extractor_raw)
            except (TypeError, ValueError):
                entities_extractor = None

        return cls(
            id=data.get("id", str(uuid.uuid4())),
            title=data.get("title", "Untitled"),
            author=data.get("author", "unknown"),
            created_at=created_at,
            updated_at=updated_at,
            tags=data.get("tags", []),
            aliases=data.get("aliases", []),
            confidence=_parse_confidence(data.get("confidence", 1.0)),
            contributors=data.get("contributors", []),
            source=data.get("source"),
            source_url=data.get("source_url"),
            supersedes=data.get("supersedes"),
            derived_from_ids=data.get("derived_from_ids", []),
            expires_at=expires_at,
            extra=extra,
            version=_parse_version(data.get("version", 1)),
            schema_version=schema_version,
            namespace=data.get("namespace"),
            access_scope=data.get("access_scope"),
            note_type=data.get("note_type"),
            status=data.get("status"),
            summaries=summaries,
            entities=data.get("entities", []),
            entities_extractor=entities_extractor,
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


def apply_lcma_defaults(metadata: KnowledgeMetadata, relative_path: Path) -> None:
    """Apply LCMA read-time defaults in-place.

    Only fills fields that are ``None`` (i.e. absent from frontmatter).
    Namespace is derived from the note's relative path unless explicitly set.
    """
    if metadata.schema_version is None:
        metadata.schema_version = 1
    if metadata.namespace is None:
        metadata.namespace = derive_namespace(relative_path)
    if metadata.access_scope is None:
        metadata.access_scope = "shared"
    if metadata.note_type is None:
        metadata.note_type = "observation"
    if metadata.status is None:
        metadata.status = "active"
    # summaries left as None if not provided — no default


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


def decode(text: str, relative_path: Path) -> KnowledgeDocument:
    """Parse note *text* into a :class:`KnowledgeDocument`.

    *relative_path* is the note's path relative to the knowledge root: it
    supplies the document's ``path`` and the namespace that LCMA defaults derive
    from. No disk access — callers pass bytes they have already read, which is
    what makes the round-trip law testable without a filesystem.

    Content is returned whole and links are parsed from the whole of it. A
    caller wanting an excerpt truncates afterwards, so that no reader's links
    depend on another reader's length limit.

    Forward-compatible by construction: unknown frontmatter keys land in
    ``metadata.extra`` and are re-emitted by :func:`encode`, so a note written
    by a newer Lithos survives a round-trip through an older one.
    """
    post = frontmatter.loads(text)
    logger.debug("Frontmatter parsed: path=%s title=%r", relative_path, post.metadata.get("title"))
    metadata = KnowledgeMetadata.from_dict(post.metadata)

    # LCMA read-time defaults (namespace derived from the relative path)
    apply_lcma_defaults(metadata, relative_path)

    # The body's H1 wins over the frontmatter title; full_content re-attaches it
    # on encode, so this is the inverse.
    title, content = extract_title_from_content(post.content)
    if not title:
        title = metadata.title

    return KnowledgeDocument(
        id=metadata.id,
        title=title,
        content=content,
        metadata=metadata,
        path=relative_path,
        links=parse_wiki_links(content),
    )


def encode(doc: KnowledgeDocument) -> str:
    """Serialise *doc* to Markdown with YAML frontmatter.

    The inverse of :func:`decode` for everything the format carries: decoding
    the result reproduces the document, modulo the read-time defaults
    :func:`apply_lcma_defaults` fills in.
    """
    post = frontmatter.Post(doc.full_content, **doc.metadata.to_dict())
    return frontmatter.dumps(post)
