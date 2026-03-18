"""Search engine - Tantivy full-text and ChromaDB semantic search."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import chromadb
import tantivy
from sentence_transformers import SentenceTransformer

from lithos.config import LithosConfig, get_config
from lithos.errors import IndexingError, SearchBackendError
from lithos.knowledge import KnowledgeDocument
from lithos.telemetry import lithos_metrics, traced

if TYPE_CHECKING:
    import numpy as np
    from chromadb.api import ClientAPI
    from numpy.typing import NDArray

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """A search result."""

    id: str
    title: str
    snippet: str
    score: float
    path: str
    source_url: str = ""
    updated_at: str = ""
    is_stale: bool = False


@dataclass
class SemanticResult:
    """A semantic search result."""

    id: str
    title: str
    snippet: str
    similarity: float
    path: str
    source_url: str = ""
    updated_at: str = ""
    is_stale: bool = False


def _compute_is_stale(expires_at_str: str) -> bool:
    """Compute staleness from an expires_at ISO string."""
    if not expires_at_str:
        return False
    try:
        expires_dt = datetime.fromisoformat(expires_at_str)
        if expires_dt.tzinfo is None:
            expires_dt = expires_dt.replace(tzinfo=timezone.utc)
        else:
            expires_dt = expires_dt.astimezone(timezone.utc)
        return datetime.now(timezone.utc) > expires_dt
    except ValueError:
        return False


def reciprocal_rank_fusion(ranked_lists: list[list[str]], k: int = 60) -> dict[str, float]:
    """Merge ranked lists of doc IDs using Reciprocal Rank Fusion.

    Args:
        ranked_lists: Each inner list is a ranking of doc IDs (best first).
        k: Constant that controls score smoothing (default: 60).

    Returns:
        Dict mapping doc_id -> RRF score (higher is better).
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


def chunk_text(text: str, chunk_size: int = 500, chunk_max: int = 1000) -> list[str]:
    """Split text into chunks at paragraph boundaries.

    Args:
        text: Text to chunk
        chunk_size: Target chunk size in characters
        chunk_max: Maximum chunk size

    Returns:
        List of text chunks
    """
    text = text.strip()
    if not text:
        return []

    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    current_chunk: list[str] = []
    current_length = 0

    # Split by paragraphs first
    paragraphs = re.split(r"\n\n+", text)

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        para_len = len(para)

        # If single paragraph exceeds max, split by sentences or words
        if para_len > chunk_max:
            # Flush current chunk
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = []
                current_length = 0

            # Try to split by sentences first
            sentences = re.split(r"(?<=[.!?])\s+", para)

            # If no sentence boundaries found (single long text), split by words
            if len(sentences) == 1 and len(para) > chunk_max:
                words = para.split()
                word_chunk: list[str] = []
                word_length = 0

                for word in words:
                    if word_length + len(word) + 1 > chunk_max and word_chunk:
                        chunks.append(" ".join(word_chunk))
                        word_chunk = []
                        word_length = 0
                    word_chunk.append(word)
                    word_length += len(word) + 1

                if word_chunk:
                    chunks.append(" ".join(word_chunk))
                continue

            # Split by sentences
            sent_chunk: list[str] = []
            sent_length = 0

            for sent in sentences:
                if sent_length + len(sent) > chunk_max and sent_chunk:
                    chunks.append(" ".join(sent_chunk))
                    sent_chunk = []
                    sent_length = 0
                sent_chunk.append(sent)
                sent_length += len(sent) + 1

            if sent_chunk:
                chunks.append(" ".join(sent_chunk))
            continue

        # Check if adding this paragraph exceeds target
        if current_length + para_len > chunk_size and current_chunk:
            chunks.append("\n\n".join(current_chunk))
            current_chunk = []
            current_length = 0

        current_chunk.append(para)
        current_length += para_len + 2  # +2 for paragraph separator

    # Flush remaining
    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks


class TantivyIndex:
    """Tantivy full-text search index."""

    SCHEMA_VERSION = "3"
    """Bumped when fields are added/removed. Triggers automatic rebuild."""

    def __init__(self, index_path: Path):
        """Initialize Tantivy index.

        Args:
            index_path: Path to store index
        """
        self.index_path = index_path
        self._index: tantivy.Index | None = None
        self._schema: tantivy.Schema | None = None
        self.needs_rebuild: bool = False

    @property
    def _schema_version_path(self) -> Path:
        return self.index_path / ".schema_version"

    def _read_schema_version(self) -> str | None:
        """Read stored schema version, or None if missing."""
        try:
            return self._schema_version_path.read_text().strip()
        except (FileNotFoundError, OSError):
            return None

    def _write_schema_version(self) -> None:
        """Write current schema version to marker file."""
        self._schema_version_path.write_text(self.SCHEMA_VERSION)

    def _build_schema(self) -> tantivy.Schema:
        """Build Tantivy schema."""
        builder = tantivy.SchemaBuilder()
        # Use raw tokenizer for id to enable exact match deletion
        builder.add_text_field("id", stored=True, tokenizer_name="raw")
        builder.add_text_field("title", stored=True, tokenizer_name="en_stem")
        builder.add_text_field("content", stored=True, tokenizer_name="en_stem")
        builder.add_text_field("path", stored=True, tokenizer_name="raw")
        builder.add_text_field("author", stored=True, tokenizer_name="raw")
        builder.add_text_field("tags", stored=True, tokenizer_name="en_stem")
        builder.add_text_field("source_url", stored=True, tokenizer_name="raw")
        builder.add_text_field("updated_at", stored=True, tokenizer_name="raw")
        builder.add_text_field("expires_at", stored=True, tokenizer_name="raw")
        return builder.build()

    def open_or_create(self) -> None:
        """Open existing index or create new one.

        Schema version is checked first (fast path). If the marker is missing
        or mismatched, the index is deleted and recreated. An exception from
        ``tantivy.Index.open()`` serves as fallback for corruption or a
        skipped/corrupted marker.
        """
        self._schema = self._build_schema()
        self.index_path.mkdir(parents=True, exist_ok=True)

        stored_version = self._read_schema_version()
        if stored_version is not None and stored_version == self.SCHEMA_VERSION:
            # Fast path: version matches, try to open existing index
            try:
                self._index = tantivy.Index(self._schema, path=str(self.index_path))
                return
            except Exception as exc:
                logger.warning(
                    "Tantivy index at %s appears corrupted (%s). Recreating.",
                    self.index_path,
                    exc,
                )
        elif stored_version is not None:
            logger.info(
                "Schema version mismatch (stored=%s, current=%s). Rebuilding index.",
                stored_version,
                self.SCHEMA_VERSION,
            )
        else:
            logger.info(
                "No schema version marker found. Rebuilding index.",
            )

        # Recreate from scratch
        shutil.rmtree(self.index_path, ignore_errors=True)
        self.index_path.mkdir(parents=True, exist_ok=True)
        self._index = tantivy.Index(self._schema, path=str(self.index_path))
        self._write_schema_version()
        self.needs_rebuild = True

    @property
    def index(self) -> tantivy.Index:
        """Get index, opening if needed."""
        if self._index is None:
            self.open_or_create()
        return self._index  # type: ignore

    @property
    def schema(self) -> tantivy.Schema:
        """Get schema."""
        if self._schema is None:
            self._schema = self._build_schema()
        return self._schema

    def add_document(self, doc: KnowledgeDocument) -> None:
        """Add or update a document in the index."""
        writer = self.index.writer(heap_size=15_000_000)

        # Delete existing document with same ID
        writer.delete_documents("id", doc.id)

        # Add new document
        writer.add_document(
            tantivy.Document(
                id=doc.id,
                title=doc.title,
                content=doc.full_content,
                path=str(doc.path),
                author=doc.metadata.author,
                tags=" ".join(doc.metadata.tags),
                source_url=doc.metadata.source_url or "",
                updated_at=doc.metadata.updated_at.isoformat() if doc.metadata.updated_at else "",
                expires_at=doc.metadata.expires_at.isoformat() if doc.metadata.expires_at else "",
            )
        )

        writer.commit()
        del writer  # Release lock
        # Reload to see changes immediately
        self.index.reload()

    def rebuild_from_docs(self, docs: list[KnowledgeDocument]) -> None:
        """Clear the index and re-index *docs* in a single commit.

        Prefer this over calling ``clear()`` + ``add_document()`` in a loop to
        avoid O(n) writer acquisitions and commits.
        """
        writer = self.index.writer(heap_size=15_000_000)
        writer.delete_all_documents()
        for doc in docs:
            writer.add_document(
                tantivy.Document(
                    id=doc.id,
                    title=doc.title,
                    content=doc.full_content,
                    path=str(doc.path),
                    author=doc.metadata.author,
                    tags=" ".join(doc.metadata.tags),
                    source_url=doc.metadata.source_url or "",
                    updated_at=(
                        doc.metadata.updated_at.isoformat() if doc.metadata.updated_at else ""
                    ),
                    expires_at=(
                        doc.metadata.expires_at.isoformat() if doc.metadata.expires_at else ""
                    ),
                )
            )
        writer.commit()
        del writer  # Release lock
        self.index.reload()

    def remove_document(self, doc_id: str) -> None:
        """Remove a document from the index."""
        writer = self.index.writer(heap_size=15_000_000)
        writer.delete_documents("id", doc_id)
        writer.commit()
        del writer  # Release lock
        # Reload to see changes immediately
        self.index.reload()

    def search(
        self,
        query: str,
        limit: int = 10,
        tags: list[str] | None = None,
        author: str | None = None,
        path_prefix: str | None = None,
    ) -> list[SearchResult]:
        """Search the index.

        Args:
            query: Search query (Tantivy query syntax)
            limit: Maximum results
            tags: Filter by tags (AND)
            author: Filter by author
            path_prefix: Filter by path prefix

        Returns:
            List of search results
        """
        self.index.reload()
        searcher = self.index.searcher()

        # Build query with filters
        query_parts = [query]
        if tags:
            for tag in tags:
                query_parts.append(f"tags:{tag}")
        if author:
            query_parts.append(f"author:{author}")

        full_query = " AND ".join(f"({p})" for p in query_parts)

        try:
            parsed_query = self.index.parse_query(full_query, ["title", "content"])
            results = searcher.search(parsed_query, limit).hits
        except Exception:
            return []

        search_results: list[SearchResult] = []
        for score, doc_address in results:
            doc = searcher.doc(doc_address)
            doc_path = doc.get_first("path")

            # Apply path prefix filter
            if path_prefix and not str(doc_path).startswith(path_prefix):
                continue

            # Generate snippet from content
            content = doc.get_first("content") or ""
            snippet = self._generate_snippet(content, query)

            expires_at_str = doc.get_first("expires_at") or ""
            search_results.append(
                SearchResult(
                    id=doc.get_first("id") or "",
                    title=doc.get_first("title") or "",
                    snippet=snippet,
                    score=score,
                    path=str(doc_path),
                    source_url=doc.get_first("source_url") or "",
                    updated_at=doc.get_first("updated_at") or "",
                    is_stale=_compute_is_stale(expires_at_str),
                )
            )

        return search_results

    def _generate_snippet(self, content: str, query: str, context_chars: int = 150) -> str:
        """Generate a snippet showing query terms in context."""
        # Extract query terms (simple tokenization)
        terms = re.findall(r"\w+", query.lower())
        content_lower = content.lower()

        # Find first occurrence of any term
        best_pos = len(content)
        for term in terms:
            pos = content_lower.find(term)
            if 0 <= pos < best_pos:
                best_pos = pos

        if best_pos == len(content):
            # No term found, return beginning
            return (
                content[: context_chars * 2] + "..."
                if len(content) > context_chars * 2
                else content
            )

        # Extract context around match
        start = max(0, best_pos - context_chars)
        end = min(len(content), best_pos + context_chars)

        snippet = content[start:end]
        if start > 0:
            snippet = "..." + snippet
        if end < len(content):
            snippet = snippet + "..."

        return snippet

    def count_docs(self) -> int:
        """Return the number of documents in the index."""
        self.index.reload()
        return self.index.searcher().num_docs

    def get_indexed_doc_ids(self) -> set[str]:
        """Return the set of doc_ids currently stored in the index."""
        try:
            self.index.reload()
            searcher = self.index.searcher()
            num = searcher.num_docs
            if num == 0:
                return set()
            # Match-all query via wildcard on the content field
            query = self.index.parse_query("*", ["content"])
            hits = searcher.search(query, num).hits
            ids: set[str] = set()
            for _score, doc_address in hits:
                doc = searcher.doc(doc_address)
                doc_id = doc.get_first("id")
                if doc_id:
                    ids.add(str(doc_id))
            return ids
        except Exception:
            logger.warning(
                "Tantivy get_indexed_doc_ids failed; treating index as empty (needs rebuild)",
                exc_info=True,
            )
            return set()

    def clear(self) -> None:
        """Clear the entire index."""
        writer = self.index.writer(heap_size=15_000_000)
        writer.delete_all_documents()
        writer.commit()
        del writer  # Release lock
        self.index.reload()


class ChromaIndex:
    """ChromaDB semantic search index."""

    def __init__(self, chroma_path: Path, model_name: str = "all-MiniLM-L6-v2"):
        """Initialize ChromaDB index.

        Args:
            chroma_path: Path to store ChromaDB data
            model_name: Sentence transformer model name
        """
        self.chroma_path = chroma_path
        self.model_name = model_name
        self._client: ClientAPI | None = None
        self._collection: chromadb.Collection | None = None
        self._model: SentenceTransformer | None = None
        self._model_lock: asyncio.Lock | None = None

    @property
    def model(self) -> SentenceTransformer:
        """Get embedding model, loading if needed (synchronous, for backward compatibility)."""
        if self._model is None:
            self._model = SentenceTransformer(self.model_name)
        return self._model

    async def ensure_model_loaded(self) -> None:
        """Ensure the embedding model is loaded, using a thread pool to avoid blocking."""
        # Lazily create the lock (safe: asyncio is single-threaded)
        if self._model_lock is None:
            self._model_lock = asyncio.Lock()
        async with self._model_lock:
            if self._model is None:
                self._model = await asyncio.to_thread(SentenceTransformer, self.model_name)

    async def embed_async(self, texts: list[str]) -> NDArray[np.float32]:
        """Generate embeddings asynchronously without blocking the event loop.

        NOTE: This method is currently unused (dead code).  It is kept here as
        preparatory work for a follow-up PR that will migrate the search layer to
        fully async embedding generation.  Remove this note once the follow-up
        lands, or remove the method if the approach changes.
        """
        await self.ensure_model_loaded()
        return await asyncio.to_thread(self.model.encode, texts)

    def health_check(self) -> None:
        """Probe the embedding model (warm-up / liveness). Raises on failure."""
        self.model.encode(["health check"])  # result discarded; exercises model load

    @property
    def client(self) -> ClientAPI:
        """Get ChromaDB client."""
        if self._client is None:
            self.chroma_path.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self.chroma_path))
        return self._client

    @property
    def collection(self) -> chromadb.Collection:
        """Get or create collection."""
        if self._collection is None:
            self._collection = self.client.get_or_create_collection(  # type: ignore[assignment]
                name="knowledge",
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection  # type: ignore[return-value]

    def _chunk_id(self, doc_id: str, chunk_index: int) -> str:
        """Generate unique ID for a chunk."""
        return f"{doc_id}__chunk_{chunk_index}"

    def add_document(
        self,
        doc: KnowledgeDocument,
        chunk_size: int = 500,
        chunk_max: int = 1000,
    ) -> int:
        """Add or update a document in the index.

        Args:
            doc: Document to add
            chunk_size: Target chunk size
            chunk_max: Maximum chunk size

        Returns:
            Number of chunks created
        """
        # Remove existing chunks for this document
        self.remove_document(doc.id)

        # Chunk the content
        full_text = f"{doc.title}\n\n{doc.content}"
        chunks = chunk_text(full_text, chunk_size, chunk_max)

        if not chunks:
            return 0

        # Generate embeddings
        embeddings = self.model.encode(chunks).tolist()

        # Prepare data for ChromaDB
        ids = [self._chunk_id(doc.id, i) for i in range(len(chunks))]
        metadatas = [
            {
                "doc_id": doc.id,
                "chunk_index": i,
                "title": doc.title,
                "path": str(doc.path),
                "author": doc.metadata.author,
                "tags": ",".join(doc.metadata.tags),
                "source_url": doc.metadata.source_url or "",
                "updated_at": (
                    doc.metadata.updated_at.isoformat() if doc.metadata.updated_at else ""
                ),
                "expires_at": (
                    doc.metadata.expires_at.isoformat() if doc.metadata.expires_at else ""
                ),
            }
            for i in range(len(chunks))
        ]

        # Add to collection
        self.collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=chunks,
            metadatas=metadatas,  # type: ignore[arg-type]
        )

        return len(chunks)

    def remove_document(self, doc_id: str) -> None:
        """Remove all chunks for a document."""
        # Query for all chunks with this doc_id
        try:
            results = self.collection.get(
                where={"doc_id": doc_id},
                include=[],
            )
            if results["ids"]:
                self.collection.delete(ids=results["ids"])
        except Exception:
            pass

    def search(
        self,
        query: str,
        limit: int = 10,
        threshold: float = 0.5,
        tags: list[str] | None = None,
        author: str | None = None,
        path_prefix: str | None = None,
    ) -> list[SemanticResult]:
        """Semantic search.

        Args:
            query: Natural language query
            limit: Maximum results
            threshold: Minimum similarity (0-1)
            tags: Filter by tags
            author: Filter by author (exact match)
            path_prefix: Filter by path prefix

        Returns:
            List of semantic results (deduplicated by document)
        """
        # Generate query embedding
        query_embedding = self.model.encode([query]).tolist()[0]

        # Build where filter
        # where_filter not used - ChromaDB filtering done post-query
        if tags:
            # ChromaDB doesn't support complex tag filtering well,
            # so we'll filter post-query
            pass

        # Query ChromaDB - get more results than needed for deduplication
        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=limit * 3,  # Get extra for deduplication
            include=["documents", "metadatas", "distances"],
        )

        # Process results and deduplicate by document
        seen_docs: set[str] = set()
        semantic_results: list[SemanticResult] = []

        if not results["ids"] or not results["ids"][0]:
            return []

        for i, _chunk_id in enumerate(results["ids"][0]):
            metadata = results["metadatas"][0][i] if results["metadatas"] else {}
            doc_id = str(metadata.get("doc_id", ""))

            # Skip if already seen this document
            if doc_id in seen_docs:
                continue

            # Calculate similarity from distance (cosine distance to similarity)
            distance = results["distances"][0][i] if results["distances"] else 1.0
            similarity = 1.0 - distance

            # Apply threshold
            if similarity < threshold:
                continue

            # Apply tag filter
            if tags:
                doc_tags = str(metadata.get("tags", "")).split(",")
                if not all(t in doc_tags for t in tags):
                    continue

            # Apply author filter
            if author and str(metadata.get("author", "")) != author:
                continue

            # Apply path prefix filter
            if path_prefix and not str(metadata.get("path", "")).startswith(path_prefix):
                continue

            seen_docs.add(doc_id)
            expires_at_str = str(metadata.get("expires_at", ""))
            semantic_results.append(
                SemanticResult(
                    id=doc_id,
                    title=str(metadata.get("title", "")),
                    snippet=results["documents"][0][i] if results["documents"] else "",
                    similarity=similarity,
                    path=str(metadata.get("path", "")),
                    source_url=str(metadata.get("source_url", "")),
                    updated_at=str(metadata.get("updated_at", "")),
                    is_stale=_compute_is_stale(expires_at_str),
                )
            )

            if len(semantic_results) >= limit:
                break

        return semantic_results

    def get_indexed_doc_ids(self) -> set[str]:
        """Return the set of unique doc_ids present in the collection."""
        try:
            result = self.collection.get(include=["metadatas"])
            metadatas = result.get("metadatas") or []
            return {str(m["doc_id"]) for m in metadatas if m and "doc_id" in m}
        except Exception:
            logger.warning(
                "ChromaDB get_indexed_doc_ids failed; treating index as empty (needs rebuild)",
                exc_info=True,
            )
            return set()

    def clear(self) -> None:
        """Clear the entire collection."""
        try:
            self.client.delete_collection("knowledge")
            self._collection = None
        except Exception:
            pass

    def count_chunks(self) -> int:
        """Get total number of chunks."""
        return self.collection.count()


class SearchEngine:
    """Combined search engine with full-text and semantic search."""

    def __init__(self, config: LithosConfig | None = None):
        """Initialize search engine.

        Args:
            config: Configuration. Uses global config if not provided.
        """
        self._config = config
        self._tantivy: TantivyIndex | None = None
        self._chroma: ChromaIndex | None = None

    @property
    def config(self) -> LithosConfig:
        """Get configuration."""
        return self._config or get_config()

    @property
    def tantivy(self) -> TantivyIndex:
        """Get Tantivy index."""
        if self._tantivy is None:
            self._tantivy = TantivyIndex(self.config.storage.tantivy_path)
            self._tantivy.open_or_create()
        return self._tantivy

    @property
    def chroma(self) -> ChromaIndex:
        """Get ChromaDB index."""
        if self._chroma is None:
            self._chroma = ChromaIndex(
                self.config.storage.chroma_path,
                self.config.search.embedding_model,
            )
        return self._chroma

    async def ensure_embeddings_loaded(self) -> None:
        """Pre-warm the embedding model asynchronously."""
        await self.chroma.ensure_model_loaded()

    @traced("lithos.search.index_document")
    def index_document(self, doc: KnowledgeDocument) -> int:
        """Index a document in both search engines.

        Partial failures (one backend succeeds) are logged as warnings.
        If *both* backends fail the operation is considered a total failure and
        ``IndexingError`` is raised.

        Returns:
            Number of chunks created for semantic search.

        Raises:
            IndexingError: If every backend failed to index the document.
        """
        errors: dict[str, Exception] = {}

        try:
            self.tantivy.add_document(doc)
        except Exception as exc:
            logger.warning("Full-text indexing failed for doc %s: %s", doc.id, exc)
            errors["tantivy"] = exc

        chunks = 0
        try:
            chunks = self.chroma.add_document(
                doc,
                self.config.search.chunk_size,
                self.config.search.chunk_max,
            )
        except Exception as exc:
            logger.warning("Semantic indexing failed for doc %s: %s", doc.id, exc)
            errors["chroma"] = exc

        if len(errors) == 2:
            raise IndexingError(
                f"All backends failed to index document {doc.id!r}",
                errors,
            )

        return chunks

    @traced("lithos.search.remove_document")
    def remove_document(self, doc_id: str) -> None:
        """Remove a document from both search engines.

        Partial failures (one backend succeeds) are logged as warnings.
        If *both* backends fail, ``IndexingError`` is raised.

        Raises:
            IndexingError: If every backend failed to remove the document.
        """
        errors: dict[str, Exception] = {}

        try:
            self.tantivy.remove_document(doc_id)
        except Exception as exc:
            logger.warning("Full-text removal failed for doc %s: %s", doc_id, exc)
            errors["tantivy"] = exc

        try:
            self.chroma.remove_document(doc_id)
        except Exception as exc:
            logger.warning("Semantic removal failed for doc %s: %s", doc_id, exc)
            errors["chroma"] = exc

        if len(errors) == 2:
            raise IndexingError(
                f"All backends failed to remove document {doc_id!r}",
                errors,
            )

    @traced("lithos.search.fulltext")
    def full_text_search(
        self,
        query: str,
        limit: int = 10,
        tags: list[str] | None = None,
        author: str | None = None,
        path_prefix: str | None = None,
    ) -> list[SearchResult]:
        """Full-text search using Tantivy.

        Raises:
            SearchBackendError: If the Tantivy backend raises an exception.
                An empty result list means *no documents matched*; this error
                means the query could not be executed at all.
        """
        start = time.perf_counter()
        success = True
        try:
            return self.tantivy.search(
                query=query,
                limit=limit,
                tags=tags,
                author=author,
                path_prefix=path_prefix,
            )
        except Exception as exc:
            success = False
            logger.error("Full-text search failed: %s", exc)
            raise SearchBackendError(
                "Full-text search backend (tantivy) failed",
                {"tantivy": exc},
            ) from exc
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            lithos_metrics.search_ops.add(1, {"type": "fulltext"})
            lithos_metrics.search_duration.record(
                elapsed_ms, {"type": "fulltext", "success": success}
            )

    @traced("lithos.search.semantic")
    def semantic_search(
        self,
        query: str,
        limit: int = 10,
        threshold: float | None = None,
        tags: list[str] | None = None,
        author: str | None = None,
        path_prefix: str | None = None,
    ) -> list[SemanticResult]:
        """Semantic search using ChromaDB.

        Args:
            query: Natural language query
            limit: Maximum results
            threshold: Minimum similarity 0-1 (default: config value)
            tags: Filter by tags (AND)
            author: Filter by author
            path_prefix: Filter by path prefix

        Raises:
            SearchBackendError: If the ChromaDB backend raises an exception.
                An empty result list means *no documents matched*; this error
                means the query could not be executed at all.
        """
        if threshold is None:
            threshold = self.config.search.semantic_threshold
        start = time.perf_counter()
        success = True
        try:
            return self.chroma.search(
                query=query,
                limit=limit,
                threshold=threshold,
                tags=tags,
                author=author,
                path_prefix=path_prefix,
            )
        except Exception as exc:
            success = False
            logger.error("Semantic search failed: %s", exc)
            raise SearchBackendError(
                "Semantic search backend (chroma) failed",
                {"chroma": exc},
            ) from exc
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            lithos_metrics.search_ops.add(1, {"type": "semantic"})
            lithos_metrics.search_duration.record(
                elapsed_ms, {"type": "semantic", "success": success}
            )

    @traced("lithos.search.hybrid")
    def hybrid_search(
        self,
        query: str,
        limit: int = 10,
        threshold: float | None = None,
        tags: list[str] | None = None,
        author: str | None = None,
        path_prefix: str | None = None,
    ) -> list[SearchResult]:
        """Hybrid search combining full-text (Tantivy) and semantic (ChromaDB) via RRF.

        Calls both backends, extracts ranked doc ID lists, merges them with
        Reciprocal Rank Fusion (k=60), and returns SearchResult objects sorted
        by RRF score descending.

        Raises:
            SearchBackendError: If every backend fails.  A single-backend failure
                is tolerated and logged — the other backend's results are returned.
        """
        start = time.perf_counter()
        success = True
        try:
            ft_results: list[SearchResult] = []
            sem_results: list[SemanticResult] = []
            ft_failed = False
            sem_failed = False

            try:
                ft_results = self.tantivy.search(
                    query=query,
                    limit=limit,
                    tags=tags,
                    author=author,
                    path_prefix=path_prefix,
                )
            except Exception as exc:
                logger.warning("Hybrid search: full-text backend failed: %s", exc)
                ft_failed = True

            try:
                sem_results = self.chroma.search(
                    query=query,
                    limit=limit,
                    threshold=threshold
                    if threshold is not None
                    else self.config.search.semantic_threshold,
                    tags=tags,
                    author=author,
                    path_prefix=path_prefix,
                )
            except Exception as exc:
                logger.warning("Hybrid search: semantic backend failed: %s", exc)
                sem_failed = True

            if ft_failed and sem_failed:
                raise SearchBackendError(
                    "All backends failed during hybrid search",
                    {},
                )

            ft_ids = [r.id for r in ft_results]
            sem_ids = [r.id for r in sem_results]

            rrf_scores = reciprocal_rank_fusion([ft_ids, sem_ids])

            # Build lookup maps for merging
            ft_by_id = {r.id: r for r in ft_results}
            sem_by_id = {r.id: r for r in sem_results}

            merged: list[SearchResult] = []
            for doc_id, rrf_score in sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True):
                if doc_id in ft_by_id:
                    r = ft_by_id[doc_id]
                    merged.append(
                        SearchResult(
                            id=r.id,
                            title=r.title,
                            snippet=r.snippet,
                            score=rrf_score,
                            path=r.path,
                            source_url=r.source_url,
                            updated_at=r.updated_at,
                            is_stale=r.is_stale,
                        )
                    )
                else:
                    s = sem_by_id[doc_id]
                    merged.append(
                        SearchResult(
                            id=s.id,
                            title=s.title,
                            snippet=s.snippet,
                            score=rrf_score,
                            path=s.path,
                            source_url=s.source_url,
                            updated_at=s.updated_at,
                            is_stale=s.is_stale,
                        )
                    )
                if len(merged) >= limit:
                    break

            return merged
        except SearchBackendError:
            success = False
            raise
        except Exception as exc:
            success = False
            logger.error("Hybrid search failed: %s", exc)
            raise SearchBackendError("Hybrid search failed", {}) from exc
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            lithos_metrics.search_ops.add(1, {"type": "hybrid"})
            lithos_metrics.search_duration.record(
                elapsed_ms, {"type": "hybrid", "success": success}
            )

    def clear_all(self) -> None:
        """Clear all indices."""
        self.tantivy.clear()
        self.chroma.clear()

    def get_stats(self) -> dict[str, int]:
        """Get search index statistics."""
        return {
            "chunks": self.chroma.count_chunks(),
        }

    def health(self) -> dict[str, str]:
        """Return a health status dict for each backend.

        Values are "ok" or a short error description.  Does not raise.
        """
        status: dict[str, str] = {}

        try:
            _ = self.tantivy.index  # triggers open_or_create if needed
            status["tantivy"] = "ok"
        except Exception as exc:
            status["tantivy"] = f"unavailable: {exc}"

        try:
            _ = self.chroma.collection.count()
            status["chroma"] = "ok"
        except Exception as exc:
            status["chroma"] = f"unavailable: {exc}"

        return status
