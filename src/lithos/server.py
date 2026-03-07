"""Lithos MCP Server - FastMCP server exposing all tools."""

import asyncio
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from lithos.config import LithosConfig, get_config, set_config
from lithos.coordination import CoordinationService
from lithos.graph import KnowledgeGraph
from lithos.knowledge import _UNSET, KnowledgeDocument, KnowledgeManager
from lithos.search import SearchEngine
from lithos.telemetry import get_tracer, register_active_claims_observer

logger = logging.getLogger(__name__)


class LithosServer:
    """Lithos MCP Server."""

    def __init__(self, config: LithosConfig | None = None):
        """Initialize server.

        Args:
            config: Configuration. Uses global config if not provided.
        """
        self._config = config or get_config()
        set_config(self._config)

        # Initialize components
        self.knowledge = KnowledgeManager()
        self.search = SearchEngine(self._config)
        self.graph = KnowledgeGraph(self._config)
        self.coordination = CoordinationService(self._config)

        # Cached active claims count for the OTEL observable gauge callback
        self._cached_active_claims: int = 0

        # File watcher
        self._observer: Observer | None = None
        self._watch_loop: asyncio.AbstractEventLoop | None = None
        self._pending_updates: set[Path] = set()
        self._update_lock = asyncio.Lock()

        # Create FastMCP app
        self.mcp = FastMCP(
            "Lithos",
            instructions="Local shared knowledge base for AI agents",
        )

        # Register all tools
        self._register_tools()

    @property
    def config(self) -> LithosConfig:
        """Get configuration."""
        return self._config

    async def initialize(self) -> None:
        """Initialize all components."""
        # Ensure directories exist
        self.config.ensure_directories()

        # Initialize coordination database
        await self.coordination.initialize()

        # Register active claims gauge observer
        register_active_claims_observer(lambda: self._cached_active_claims)

        # Load or build indices.
        # Force access to the tantivy property so schema version check runs.
        tantivy_needs_rebuild = self.search.tantivy.needs_rebuild
        if self.config.index.rebuild_on_start or tantivy_needs_rebuild:
            await self._rebuild_indices()
        else:
            # Try to load cached graph
            if not self.graph.load_cache():
                await self._rebuild_indices()

    async def _rebuild_indices(self) -> None:
        """Rebuild all search indices from files."""
        tracer = get_tracer()
        with tracer.start_as_current_span("lithos.index.rebuild") as span:
            self.search.clear_all()
            self.graph.clear()

            knowledge_path = self.config.storage.knowledge_path
            file_count = 0
            error_count = 0
            for file_path in knowledge_path.rglob("*.md"):
                try:
                    relative_path = file_path.relative_to(knowledge_path)
                    doc, _ = await self.knowledge.read(path=str(relative_path))
                    self.search.index_document(doc)
                    self.graph.add_document(doc)
                    file_count += 1
                except Exception as e:
                    error_count += 1
                    logger.error("Error indexing %s: %s", file_path, e)

            span.set_attribute("lithos.file_count", file_count)
            span.set_attribute("lithos.error_count", error_count)
            self.graph.save_cache()

    def start_file_watcher(self) -> None:
        """Start watching for file changes."""
        if self._observer:
            return

        try:
            self._watch_loop = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError("File watcher requires a running asyncio event loop") from None

        handler = _FileChangeHandler(self, self._watch_loop)
        self._observer = Observer()
        self._observer.schedule(
            handler,
            str(self.config.storage.knowledge_path),
            recursive=True,
        )
        self._observer.start()

    def stop_file_watcher(self) -> None:
        """Stop file watcher."""
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None

    async def handle_file_change(self, path: Path, deleted: bool = False) -> None:
        """Handle a file change event."""
        if path.suffix != ".md":
            return

        tracer = get_tracer()
        with tracer.start_as_current_span("lithos.file_change") as span:
            span.set_attribute("lithos.deleted", deleted)
            async with self._update_lock:
                try:
                    knowledge_path = self.config.storage.knowledge_path
                    try:
                        relative_path = path.relative_to(knowledge_path)
                    except ValueError:
                        return

                    if deleted:
                        doc_id = self.knowledge.get_id_by_path(relative_path)
                        if doc_id:
                            await self.knowledge.delete(doc_id)
                            self.search.remove_document(doc_id)
                            self.graph.remove_document(doc_id)
                            self.graph.save_cache()
                    else:
                        doc, _ = await self.knowledge.read(path=str(relative_path))
                        self.search.index_document(doc)
                        self.graph.add_document(doc)
                        self.graph.save_cache()
                except Exception as e:
                    logger.error("Error handling file change %s: %s", path, e)

    def _register_tools(self) -> None:
        """Register all MCP tools."""

        # ==================== Knowledge Tools ====================

        @self.mcp.tool()
        async def lithos_write(
            title: str,
            content: str,
            agent: str,
            tags: list[str] | None = None,
            confidence: float | None = None,
            path: str | None = None,
            id: str | None = None,
            source_task: str | None = None,
            source_url: str | None = None,
        ) -> dict[str, Any]:
            """Create or update a knowledge file.

            Args:
                title: Title of the knowledge item
                content: Markdown content (without frontmatter)
                agent: Your agent identifier
                tags: List of tags
                confidence: Confidence score 0-1 (default: 1.0 on create, preserved on update)
                path: Subdirectory path (e.g., "procedures")
                id: UUID to update existing; omit to create new
                source_task: Task ID this knowledge came from
                source_url: URL provenance for this knowledge. Pass "" to clear an
                    existing source_url on update.

            Returns:
                Dict with status envelope: created/updated/duplicate
            """
            logger.info("lithos_write agent=%s title=%r update=%s", agent, title, id is not None)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.write") as span:
                span.set_attribute("lithos.tool", "lithos_write")
                span.set_attribute("lithos.agent", agent)
                span.set_attribute("lithos.is_update", id is not None)

                await self.coordination.ensure_agent_known(agent)

                warnings: list[str] = []

                if id:
                    # Update existing — map MCP boundary to manager semantics:
                    # None (omitted) → _UNSET (preserve), "" → None (clear), str → pass through
                    if source_url is None:
                        url_arg = _UNSET
                    elif source_url == "":
                        url_arg = None
                    else:
                        url_arg = source_url
                    result = await self.knowledge.update(
                        id=id,
                        agent=agent,
                        title=title,
                        content=content,
                        tags=tags,
                        confidence=confidence,
                        source_url=url_arg,
                    )
                else:
                    # Create new — default confidence to 1.0 when not specified
                    result = await self.knowledge.create(
                        title=title,
                        content=content,
                        agent=agent,
                        tags=tags,
                        confidence=confidence if confidence is not None else 1.0,
                        path=path,
                        source=source_task,
                        source_url=source_url or None,
                    )

                # Handle dict results (duplicate or invalid_input)
                if isinstance(result, dict):
                    status = result.get("status", "error")
                    if status == "duplicate":
                        span.set_attribute("lithos.write_status", "duplicate")
                        return {
                            "status": "duplicate",
                            "duplicate_of": result["duplicate_of"],
                            "message": result["message"],
                            "warnings": warnings,
                        }
                    elif status == "invalid_input":
                        span.set_attribute("lithos.write_status", "invalid_input")
                        return {
                            "status": "error",
                            "code": "invalid_input",
                            "message": result["message"],
                            "warnings": warnings,
                        }

                doc: KnowledgeDocument = result
                status_label = "updated" if id else "created"

                # Update indices
                self.search.index_document(doc)
                self.graph.add_document(doc)
                self.graph.save_cache()

                span.set_attribute("lithos.doc_id", doc.id)
                span.set_attribute("lithos.write_status", status_label)
                logger.info("lithos_write completed doc_id=%s status=%s", doc.id, status_label)
                return {
                    "status": status_label,
                    "id": doc.id,
                    "path": str(doc.path),
                    "warnings": warnings,
                }

        @self.mcp.tool()
        async def lithos_read(
            id: str | None = None,
            path: str | None = None,
            max_length: int | None = None,
        ) -> dict[str, Any]:
            """Read a knowledge file by ID or path.

            Args:
                id: UUID of knowledge item
                path: File path relative to knowledge/
                max_length: Truncate content to N characters

            Returns:
                Dict with id, title, content, metadata, links, truncated
            """
            logger.info("lithos_read id=%s path=%s", id, path)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.read") as span:
                span.set_attribute("lithos.tool", "lithos_read")
                if id:
                    span.set_attribute("lithos.id", id)

                doc, truncated = await self.knowledge.read(
                    id=id,
                    path=path,
                    max_length=max_length,
                )

                span.set_attribute("lithos.truncated", truncated)
                meta = doc.metadata.to_dict()
                meta["source_url"] = doc.metadata.source_url  # null when None
                return {
                    "id": doc.id,
                    "title": doc.title,
                    "content": doc.content,
                    "metadata": meta,
                    "links": [
                        {"target": link.target, "display": link.display} for link in doc.links
                    ],
                    "truncated": truncated,
                }

        @self.mcp.tool()
        async def lithos_delete(
            id: str,
            agent: str | None = None,
        ) -> dict[str, bool]:
            """Delete a knowledge file.

            Args:
                id: UUID of knowledge item to delete
                agent: Agent performing deletion (for audit trail)

            Returns:
                Dict with success boolean
            """
            logger.info("lithos_delete id=%s agent=%s", id, agent)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.delete") as span:
                span.set_attribute("lithos.tool", "lithos_delete")
                span.set_attribute("lithos.id", id)
                if agent:
                    span.set_attribute("lithos.agent", agent)
                    await self.coordination.ensure_agent_known(agent)

                success = await self.knowledge.delete(id)

                if success:
                    self.search.remove_document(id)
                    self.graph.remove_document(id)
                    self.graph.save_cache()

                return {"success": success}

        @self.mcp.tool()
        async def lithos_search(
            query: str,
            limit: int = 10,
            tags: list[str] | None = None,
            author: str | None = None,
            path_prefix: str | None = None,
        ) -> dict[str, list[dict[str, Any]]]:
            """Full-text search across knowledge base.

            Args:
                query: Search query (Tantivy query syntax)
                limit: Max results (default: 10)
                tags: Filter by tags (AND)
                author: Filter by author
                path_prefix: Filter by path prefix

            Returns:
                Dict with results list containing id, title, snippet, score, path
            """
            logger.info("lithos_search query_len=%d limit=%d", len(query), limit)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.search") as span:
                span.set_attribute("lithos.tool", "lithos_search")
                span.set_attribute("lithos.query.length", len(query))
                span.set_attribute(
                    "lithos.query.sha256",
                    hashlib.sha256(query.encode()).hexdigest()[:16],
                )
                span.set_attribute("lithos.limit", limit)

                results = self.search.full_text_search(
                    query=query,
                    limit=limit,
                    tags=tags,
                    author=author,
                    path_prefix=path_prefix,
                )

                span.set_attribute("lithos.result_count", len(results))
                logger.info("lithos_search results=%d", len(results))
                return {
                    "results": [
                        {
                            "id": r.id,
                            "title": r.title,
                            "snippet": r.snippet,
                            "score": r.score,
                            "path": r.path,
                            "source_url": r.source_url,
                            "updated_at": r.updated_at,
                            "is_stale": r.is_stale,
                        }
                        for r in results
                    ]
                }

        @self.mcp.tool()
        async def lithos_semantic(
            query: str,
            limit: int = 10,
            threshold: float | None = None,
            tags: list[str] | None = None,
        ) -> dict[str, list[dict[str, Any]]]:
            """Semantic similarity search.

            Args:
                query: Natural language query
                limit: Max results (default: 10)
                threshold: Minimum similarity 0-1 (default: 0.5)
                tags: Filter by tags (AND)

            Returns:
                Dict with results list containing id, title, snippet, similarity, path
            """
            logger.info("lithos_semantic query_len=%d limit=%d", len(query), limit)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.semantic") as span:
                span.set_attribute("lithos.tool", "lithos_semantic")
                span.set_attribute("lithos.query.length", len(query))
                span.set_attribute(
                    "lithos.query.sha256",
                    hashlib.sha256(query.encode()).hexdigest()[:16],
                )
                span.set_attribute("lithos.limit", limit)

                results = self.search.semantic_search(
                    query=query,
                    limit=limit,
                    threshold=threshold,
                    tags=tags,
                )

                span.set_attribute("lithos.result_count", len(results))
                logger.info("lithos_semantic results=%d", len(results))
                return {
                    "results": [
                        {
                            "id": r.id,
                            "title": r.title,
                            "snippet": r.snippet,
                            "similarity": r.similarity,
                            "path": r.path,
                            "source_url": r.source_url,
                            "updated_at": r.updated_at,
                            "is_stale": r.is_stale,
                        }
                        for r in results
                    ]
                }

        @self.mcp.tool()
        async def lithos_list(
            path_prefix: str | None = None,
            tags: list[str] | None = None,
            author: str | None = None,
            since: str | None = None,
            limit: int = 50,
            offset: int = 0,
        ) -> dict[str, Any]:
            """List knowledge documents with filters.

            Args:
                path_prefix: Filter by path prefix
                tags: Filter by tags (AND)
                author: Filter by author
                since: Filter by updated since (ISO datetime)
                limit: Max results (default: 50)
                offset: Pagination offset

            Returns:
                Dict with items list and total count
            """
            logger.info("lithos_list limit=%d offset=%d", limit, offset)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.list") as span:
                span.set_attribute("lithos.tool", "lithos_list")
                span.set_attribute("lithos.limit", limit)

                since_dt = None
                if since:
                    since_dt = datetime.fromisoformat(since)

                docs, total = await self.knowledge.list_all(
                    path_prefix=path_prefix,
                    tags=tags,
                    author=author,
                    since=since_dt,
                    limit=limit,
                    offset=offset,
                )

                span.set_attribute("lithos.result_count", len(docs))
                logger.info("lithos_list results=%d total=%d", len(docs), total)
                return {
                    "items": [
                        {
                            "id": d.id,
                            "title": d.title,
                            "path": str(d.path),
                            "updated": d.metadata.updated_at.isoformat(),
                            "tags": d.metadata.tags,
                            "source_url": d.metadata.source_url or "",
                        }
                        for d in docs
                    ],
                    "total": total,
                }

        # ==================== Graph Tools ====================

        @self.mcp.tool()
        async def lithos_links(
            id: str,
            direction: str = "both",
            depth: int = 1,
        ) -> dict[str, list[dict[str, str]]]:
            """Get links for a document.

            Args:
                id: Document UUID
                direction: "outgoing", "incoming", or "both"
                depth: Traversal depth 1-3 (default: 1)

            Returns:
                Dict with outgoing and incoming lists of {id, title}
            """
            logger.info("lithos_links id=%s direction=%s depth=%d", id, direction, depth)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.links") as span:
                span.set_attribute("lithos.tool", "lithos_links")
                span.set_attribute("lithos.id", id)
                span.set_attribute("lithos.direction", direction)
                span.set_attribute("lithos.depth", depth)

                if direction not in ("outgoing", "incoming", "both"):
                    direction = "both"

                links = self.graph.get_links(
                    doc_id=id,
                    direction=direction,  # type: ignore
                    depth=depth,
                )

                return {
                    "outgoing": [{"id": link.id, "title": link.title} for link in links.outgoing],
                    "incoming": [{"id": link.id, "title": link.title} for link in links.incoming],
                }

        @self.mcp.tool()
        async def lithos_tags() -> dict[str, dict[str, int]]:
            """Get all tags with document counts.

            Returns:
                Dict with tags mapping tag name to count
            """
            logger.info("lithos_tags")
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.tags") as span:
                span.set_attribute("lithos.tool", "lithos_tags")
                tags = await self.knowledge.get_all_tags()
                span.set_attribute("lithos.tag_count", len(tags))
                return {"tags": tags}

        # ==================== Agent Tools ====================

        @self.mcp.tool()
        async def lithos_agent_register(
            id: str,
            name: str | None = None,
            type: str | None = None,
            metadata: dict[str, Any] | None = None,
        ) -> dict[str, bool]:
            """Register or update an agent.

            Args:
                id: Agent identifier
                name: Human-friendly display name
                type: Agent type (e.g., "agent-zero", "claude-code")
                metadata: Optional extra info

            Returns:
                Dict with success and created booleans
            """
            logger.info("lithos_agent_register id=%s type=%s", id, type)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.agent_register") as span:
                span.set_attribute("lithos.tool", "lithos_agent_register")
                span.set_attribute("lithos.agent.id", id)
                success, created = await self.coordination.register_agent(
                    agent_id=id,
                    name=name,
                    agent_type=type,
                    metadata=metadata,
                )
                span.set_attribute("lithos.created", created)
                return {"success": success, "created": created}

        @self.mcp.tool()
        async def lithos_agent_info(
            id: str,
        ) -> dict[str, Any] | None:
            """Get agent information.

            Args:
                id: Agent identifier

            Returns:
                Agent info dict or None if not found
            """
            logger.info("lithos_agent_info id=%s", id)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.agent_info") as span:
                span.set_attribute("lithos.tool", "lithos_agent_info")
                span.set_attribute("lithos.agent.id", id)
                agent = await self.coordination.get_agent(id)
                if not agent:
                    return None

                return {
                    "id": agent.id,
                    "name": agent.name,
                    "type": agent.type,
                    "first_seen_at": (
                        agent.first_seen_at.isoformat() if agent.first_seen_at else None
                    ),
                    "last_seen_at": (
                        agent.last_seen_at.isoformat() if agent.last_seen_at else None
                    ),
                    "metadata": agent.metadata,
                }

        @self.mcp.tool()
        async def lithos_agent_list(
            type: str | None = None,
            active_since: str | None = None,
        ) -> dict[str, list[dict[str, Any]]]:
            """List all known agents.

            Args:
                type: Filter by agent type
                active_since: Filter by last activity (ISO datetime)

            Returns:
                Dict with agents list
            """
            logger.info("lithos_agent_list type=%s", type)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.agent_list") as span:
                span.set_attribute("lithos.tool", "lithos_agent_list")

                since_dt = None
                if active_since:
                    since_dt = datetime.fromisoformat(active_since)

                agents = await self.coordination.list_agents(
                    agent_type=type,
                    active_since=since_dt,
                )

                span.set_attribute("lithos.result_count", len(agents))
                return {
                    "agents": [
                        {
                            "id": a.id,
                            "name": a.name,
                            "type": a.type,
                            "last_seen_at": (
                                a.last_seen_at.isoformat() if a.last_seen_at else None
                            ),
                        }
                        for a in agents
                    ]
                }

        # ==================== Coordination Tools ====================

        @self.mcp.tool()
        async def lithos_task_create(
            title: str,
            agent: str,
            description: str | None = None,
            tags: list[str] | None = None,
        ) -> dict[str, str]:
            """Create a new coordination task.

            Args:
                title: Task title
                agent: Creating agent identifier
                description: Task description
                tags: Task tags

            Returns:
                Dict with task_id
            """
            logger.info("lithos_task_create agent=%s title=%r", agent, title)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_create") as span:
                span.set_attribute("lithos.tool", "lithos_task_create")
                span.set_attribute("lithos.agent", agent)
                task_id = await self.coordination.create_task(
                    title=title,
                    agent=agent,
                    description=description,
                    tags=tags,
                )
                span.set_attribute("lithos.task_id", task_id)
                return {"task_id": task_id}

        @self.mcp.tool()
        async def lithos_task_claim(
            task_id: str,
            aspect: str,
            agent: str,
            ttl_minutes: int = 60,
        ) -> dict[str, Any]:
            """Claim an aspect of a task.

            Args:
                task_id: Task ID
                aspect: Aspect being claimed (e.g., "research", "implementation")
                agent: Agent making the claim
                ttl_minutes: Claim duration in minutes (default: 60, max: 480)

            Returns:
                Dict with success and expires_at
            """
            logger.info("lithos_task_claim task=%s aspect=%s agent=%s", task_id, aspect, agent)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_claim") as span:
                span.set_attribute("lithos.tool", "lithos_task_claim")
                span.set_attribute("lithos.agent", agent)
                span.set_attribute("lithos.task_id", task_id)
                span.set_attribute("lithos.aspect", aspect)
                success, expires_at = await self.coordination.claim_task(
                    task_id=task_id,
                    aspect=aspect,
                    agent=agent,
                    ttl_minutes=ttl_minutes,
                )
                span.set_attribute("lithos.success", success)
                return {
                    "success": success,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                }

        @self.mcp.tool()
        async def lithos_task_renew(
            task_id: str,
            aspect: str,
            agent: str,
            ttl_minutes: int = 60,
        ) -> dict[str, Any]:
            """Renew an existing claim.

            Args:
                task_id: Task ID
                aspect: Claimed aspect
                agent: Agent that owns the claim
                ttl_minutes: New duration in minutes

            Returns:
                Dict with success and new_expires_at
            """
            logger.info("lithos_task_renew task=%s aspect=%s agent=%s", task_id, aspect, agent)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_renew") as span:
                span.set_attribute("lithos.tool", "lithos_task_renew")
                span.set_attribute("lithos.agent", agent)
                span.set_attribute("lithos.task_id", task_id)
                span.set_attribute("lithos.aspect", aspect)
                success, new_expires = await self.coordination.renew_claim(
                    task_id=task_id,
                    aspect=aspect,
                    agent=agent,
                    ttl_minutes=ttl_minutes,
                )
                span.set_attribute("lithos.success", success)
                return {
                    "success": success,
                    "new_expires_at": (new_expires.isoformat() if new_expires else None),
                }

        @self.mcp.tool()
        async def lithos_task_release(
            task_id: str,
            aspect: str,
            agent: str,
        ) -> dict[str, bool]:
            """Release a claim.

            Args:
                task_id: Task ID
                aspect: Claimed aspect
                agent: Agent releasing the claim

            Returns:
                Dict with success boolean
            """
            logger.info("lithos_task_release task=%s aspect=%s agent=%s", task_id, aspect, agent)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_release") as span:
                span.set_attribute("lithos.tool", "lithos_task_release")
                span.set_attribute("lithos.agent", agent)
                span.set_attribute("lithos.task_id", task_id)
                span.set_attribute("lithos.aspect", aspect)
                success = await self.coordination.release_claim(
                    task_id=task_id,
                    aspect=aspect,
                    agent=agent,
                )
                span.set_attribute("lithos.success", success)
                return {"success": success}

        @self.mcp.tool()
        async def lithos_task_complete(
            task_id: str,
            agent: str,
        ) -> dict[str, bool]:
            """Mark a task as completed.

            Args:
                task_id: Task ID
                agent: Agent completing the task

            Returns:
                Dict with success boolean
            """
            logger.info("lithos_task_complete task=%s agent=%s", task_id, agent)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_complete") as span:
                span.set_attribute("lithos.tool", "lithos_task_complete")
                span.set_attribute("lithos.agent", agent)
                span.set_attribute("lithos.task_id", task_id)
                success = await self.coordination.complete_task(
                    task_id=task_id,
                    agent=agent,
                )
                span.set_attribute("lithos.success", success)
                return {"success": success}

        @self.mcp.tool()
        async def lithos_task_status(
            task_id: str | None = None,
        ) -> dict[str, list[dict[str, Any]]]:
            """Get task status with active claims.

            Args:
                task_id: Specific task ID, or None for all active tasks

            Returns:
                Dict with tasks list containing id, title, status, claims
            """
            logger.info("lithos_task_status task_id=%s", task_id)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_status") as span:
                span.set_attribute("lithos.tool", "lithos_task_status")
                if task_id:
                    span.set_attribute("lithos.task_id", task_id)

                statuses = await self.coordination.get_task_status(task_id)

                return {
                    "tasks": [
                        {
                            "id": s.id,
                            "title": s.title,
                            "status": s.status,
                            "claims": [
                                {
                                    "agent": c.agent,
                                    "aspect": c.aspect,
                                    "expires_at": c.expires_at.isoformat(),
                                }
                                for c in s.claims
                            ],
                        }
                        for s in statuses
                    ]
                }

        @self.mcp.tool()
        async def lithos_finding_post(
            task_id: str,
            agent: str,
            summary: str,
            knowledge_id: str | None = None,
        ) -> dict[str, str]:
            """Post a finding to a task.

            Args:
                task_id: Task ID
                agent: Agent posting the finding
                summary: Finding summary
                knowledge_id: Optional linked knowledge document ID

            Returns:
                Dict with finding_id
            """
            logger.info("lithos_finding_post task=%s agent=%s", task_id, agent)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.finding_post") as span:
                span.set_attribute("lithos.tool", "lithos_finding_post")
                span.set_attribute("lithos.agent", agent)
                span.set_attribute("lithos.task_id", task_id)
                finding_id = await self.coordination.post_finding(
                    task_id=task_id,
                    agent=agent,
                    summary=summary,
                    knowledge_id=knowledge_id,
                )
                span.set_attribute("lithos.finding_id", finding_id)
                return {"finding_id": finding_id}

        @self.mcp.tool()
        async def lithos_finding_list(
            task_id: str,
            since: str | None = None,
        ) -> dict[str, list[dict[str, Any]]]:
            """List findings for a task.

            Args:
                task_id: Task ID
                since: Filter by created since (ISO datetime)

            Returns:
                Dict with findings list
            """
            logger.info("lithos_finding_list task=%s", task_id)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.finding_list") as span:
                span.set_attribute("lithos.tool", "lithos_finding_list")
                span.set_attribute("lithos.task_id", task_id)

                since_dt = None
                if since:
                    since_dt = datetime.fromisoformat(since)

                findings = await self.coordination.list_findings(
                    task_id=task_id,
                    since=since_dt,
                )

                span.set_attribute("lithos.result_count", len(findings))
                return {
                    "findings": [
                        {
                            "id": f.id,
                            "agent": f.agent,
                            "summary": f.summary,
                            "knowledge_id": f.knowledge_id,
                            "created_at": (f.created_at.isoformat() if f.created_at else None),
                        }
                        for f in findings
                    ]
                }

        # ==================== System Tools ====================

        @self.mcp.tool()
        async def lithos_stats() -> dict[str, int]:
            """Get knowledge base statistics.

            Returns:
                Dict with documents, chunks, agents, active_tasks, open_claims, tags counts
            """
            logger.info("lithos_stats")
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.stats") as span:
                span.set_attribute("lithos.tool", "lithos_stats")

                # Get document count
                _, total_docs = await self.knowledge.list_all(limit=0)

                # Get search stats
                search_stats = self.search.get_stats()

                # Get coordination stats
                coord_stats = await self.coordination.get_stats()

                # Update cached active claims for OTEL gauge
                self._cached_active_claims = coord_stats.get("open_claims", 0)

                # Get tag count
                tags = await self.knowledge.get_all_tags()

                return {
                    "documents": total_docs,
                    "chunks": search_stats.get("chunks", 0),
                    "agents": coord_stats.get("agents", 0),
                    "active_tasks": coord_stats.get("active_tasks", 0),
                    "open_claims": coord_stats.get("open_claims", 0),
                    "tags": len(tags),
                    "duplicate_urls": self.knowledge.duplicate_url_count,
                }


class _FileChangeHandler(FileSystemEventHandler):
    """Handle file system events for index updates."""

    def __init__(self, server: LithosServer, loop: asyncio.AbstractEventLoop):
        self.server = server
        self._loop = loop

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule_update(Path(event.src_path))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule_update(Path(event.src_path))

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule_update(Path(event.src_path), deleted=True)

    def _schedule_update(self, path: Path, deleted: bool = False) -> None:
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.server.handle_file_change(path, deleted),
                self._loop,
            )
            future.add_done_callback(self._log_future_exception)
        except Exception:
            pass

    @staticmethod
    def _log_future_exception(future: asyncio.Future) -> None:
        try:
            exception = future.exception()
            if exception:
                logger.error("Error processing file update: %s", exception)
        except Exception:
            pass


# Global server instance
_server: LithosServer | None = None


def get_server() -> LithosServer:
    """Get or create the global server instance."""
    global _server
    if _server is None:
        _server = LithosServer()
    return _server


def create_server(config: LithosConfig | None = None) -> LithosServer:
    """Create a new server instance."""
    global _server
    _server = LithosServer(config)
    return _server
