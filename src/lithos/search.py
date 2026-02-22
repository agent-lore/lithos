"""Search engine - Tantivy full-text and ChromaDB semantic search."""

import re
from dataclasses import dataclass
from pathlib import Path

import chromadb
import tantivy
from sentence_transformers import SentenceTransformer

from exogram.config import ExogramConfig, get_config
from exogram.knowledge import KnowledgeDocument


@dataclass
class SearchResult:
    """A search result."""

    id: str
    title: str
    snippet: str
    score: float
    path: str


@dataclass
class SemanticResult:
    """A semantic search result."""

    id: str
    title: str
    snippet: str
    similarity: float
    path: str


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

    def __init__(self, index_path: Path):
        """Initialize Tantivy index.

        Args:
            index_path: Path to store index
        """
        self.index_path = index_path
        self._index: tantivy.Index | None = None
        self._schema: tantivy.Schema | None = None

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
        return builder.build()

    def open_or_create(self) -> None:
        """Open existing index or create new one."""
        self._schema = self._build_schema()
        self.index_path.mkdir(parents=True, exist_ok=True)

        try:
            self._index = tantivy.Index(self._schema, path=str(self.index_path))
        except Exception:
            # Index doesn't exist or is corrupted, create new
            self._index = tantivy.Index(self._schema, path=str(self.index_path))

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
            )
        )

        writer.commit()
        del writer  # Release lock
        # Reload to see changes immediately
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

            search_results.append(
                SearchResult(
                    id=doc.get_first("id") or "",
                    title=doc.get_first("title") or "",
                    snippet=snippet,
                    score=score,
                    path=str(doc_path),
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
        self._client: chromadb.PersistentClient | None = None
        self._collection: chromadb.Collection | None = None
        self._model: SentenceTransformer | None = None

    @property
    def model(self) -> SentenceTransformer:
        """Get embedding model, loading if needed."""
        if self._model is None:
            self._model = SentenceTransformer(self.model_name)
        return self._model

    @property
    def client(self) -> chromadb.PersistentClient:
        """Get ChromaDB client."""
        if self._client is None:
            self.chroma_path.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self.chroma_path))
        return self._client

    @property
    def collection(self) -> chromadb.Collection:
        """Get or create collection."""
        if self._collection is None:
            self._collection = self.client.get_or_create_collection(
                name="knowledge",
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

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
            }
            for i in range(len(chunks))
        ]

        # Add to collection
        self.collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=chunks,
            metadatas=metadatas,
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
    ) -> list[SemanticResult]:
        """Semantic search.

        Args:
            query: Natural language query
            limit: Maximum results
            threshold: Minimum similarity (0-1)
            tags: Filter by tags

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
            doc_id = metadata.get("doc_id", "")

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
                doc_tags = metadata.get("tags", "").split(",")
                if not all(t in doc_tags for t in tags):
                    continue

            seen_docs.add(doc_id)
            semantic_results.append(
                SemanticResult(
                    id=doc_id,
                    title=metadata.get("title", ""),
                    snippet=results["documents"][0][i] if results["documents"] else "",
                    similarity=similarity,
                    path=metadata.get("path", ""),
                )
            )

            if len(semantic_results) >= limit:
                break

        return semantic_results

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

    def __init__(self, config: ExogramConfig | None = None):
        """Initialize search engine.

        Args:
            config: Configuration. Uses global config if not provided.
        """
        self._config = config
        self._tantivy: TantivyIndex | None = None
        self._chroma: ChromaIndex | None = None

    @property
    def config(self) -> ExogramConfig:
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

    def index_document(self, doc: KnowledgeDocument) -> int:
        """Index a document in both search engines.

        Returns:
            Number of chunks created for semantic search
        """
        self.tantivy.add_document(doc)
        chunks = self.chroma.add_document(
            doc,
            self.config.search.chunk_size,
            self.config.search.chunk_max,
        )
        return chunks

    def remove_document(self, doc_id: str) -> None:
        """Remove a document from both search engines."""
        self.tantivy.remove_document(doc_id)
        self.chroma.remove_document(doc_id)

    def full_text_search(
        self,
        query: str,
        limit: int = 10,
        tags: list[str] | None = None,
        author: str | None = None,
        path_prefix: str | None = None,
    ) -> list[SearchResult]:
        """Full-text search using Tantivy."""
        return self.tantivy.search(
            query=query,
            limit=limit,
            tags=tags,
            author=author,
            path_prefix=path_prefix,
        )

    def semantic_search(
        self,
        query: str,
        limit: int = 10,
        threshold: float | None = None,
        tags: list[str] | None = None,
    ) -> list[SemanticResult]:
        """Semantic search using ChromaDB."""
        if threshold is None:
            threshold = self.config.search.semantic_threshold
        return self.chroma.search(
            query=query,
            limit=limit,
            threshold=threshold,
            tags=tags,
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
