"""Lithos MCP Server - FastMCP server exposing all tools."""

import asyncio
import collections
import concurrent.futures
import hashlib
import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from lithos.config import LithosConfig, get_config, set_config
from lithos.coordination import CoordinationService
from lithos.errors import SearchBackendError, SlugCollisionError
from lithos.events import (
    AGENT_REGISTERED,
    FINDING_POSTED,
    NOTE_CREATED,
    NOTE_DELETED,
    NOTE_UPDATED,
    TASK_CANCELLED,
    TASK_CLAIMED,
    TASK_COMPLETED,
    TASK_CREATED,
    TASK_RELEASED,
    EventBus,
    LithosEvent,
)
from lithos.graph import KnowledgeGraph
from lithos.knowledge import _UNSET, KnowledgeManager, _UnsetType
from lithos.search import SearchEngine
from lithos.telemetry import (
    StatusCode,
    get_tracer,
    lithos_metrics,
    register_active_claims_observer,
    register_resource_gauges,
    register_sse_active_clients_observer,
    tool_metrics,
)

logger = logging.getLogger(__name__)


class LithosServer:
    """Lithos MCP Server."""

    def __init__(self, config: LithosConfig | None = None):
        """Initialize server.

        Args:
            config: LithosConfig instance to use.  If omitted, ``get_config()``
                is called to obtain the current global config.  The resolved
                config is then stored and passed explicitly to all components
                (including :class:`~lithos.knowledge.KnowledgeManager`) — no
                component performs its own global look-up after this point.
        """
        self._config = config or get_config()
        set_config(self._config)

        # Initialize components — all receive self._config explicitly.
        self.knowledge = KnowledgeManager(self._config)
        self.search = SearchEngine(self._config)
        self.graph = KnowledgeGraph(self._config)
        self.coordination = CoordinationService(self._config)
        self.event_bus = EventBus(self._config.events)

        # Cached count fields for synchronous OTEL observable gauge callbacks
        self._cached_active_claims: int = 0
        self._cached_agent_count: int = 0

        # Background tasks (kept to prevent garbage collection)
        self._background_tasks: set[asyncio.Task[None]] = set()

        # File watcher
        self._observer: Observer | None = None  # type: ignore[reportInvalidTypeForm]
        self._watch_loop: asyncio.AbstractEventLoop | None = None
        self._pending_updates: set[Path] = set()
        self._update_lock = asyncio.Lock()

        # Create FastMCP app
        self.mcp = FastMCP(
            "Lithos",
            instructions="Local shared knowledge base for AI agents",
        )

        # SSE delivery: track active client count
        self._sse_client_count: int = 0

        # Register all tools
        self._register_tools()

        # Mount SSE delivery endpoint
        self.mcp.custom_route("/events", methods=["GET"])(self._sse_endpoint)

        # Mount HTTP health endpoint
        self.mcp.custom_route("/health", methods=["GET"])(self._health_endpoint)

        # Mount read-access audit log endpoint
        self.mcp.custom_route("/audit", methods=["GET"])(self._audit_endpoint)

    @property
    def config(self) -> LithosConfig:
        """Get configuration."""
        return self._config

    async def _emit(self, event: LithosEvent) -> None:
        """Emit an event, logging any failure without propagating."""
        try:
            await self.event_bus.emit(event)
        except Exception:
            logger.exception("Failed to emit %s event", event.type)

    async def _get_health(self) -> dict[str, Any]:
        """Run health checks and return a status dict (shared by HTTP and any callers)."""
        components: dict[str, Any] = {}

        # Check KB directory — Path.exists() returns bool, does not raise
        kb_path = self.knowledge.knowledge_path
        if not kb_path.exists():
            components["kb_directory"] = {
                "status": "unavailable",
                "error": "directory does not exist",
            }
        else:
            components["kb_directory"] = {"status": "ok"}

        # Check embedding model
        try:
            await asyncio.to_thread(self.search.chroma.health_check)
            components["embedding_model"] = {"status": "ok"}
        except Exception as e:
            components["embedding_model"] = {"status": "unavailable", "error": str(e)}

        # Check knowledge base
        try:
            await self.knowledge.list_all(limit=1)
            components["knowledge_base"] = {"status": "ok"}
        except Exception as e:
            components["knowledge_base"] = {"status": "unavailable", "error": str(e)}

        overall = "ok" if all(c["status"] == "ok" for c in components.values()) else "degraded"
        return {
            "status": overall,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "components": components,
        }

    async def _health_endpoint(self, request: Request) -> Response:
        """Lightweight HTTP health endpoint for Docker HEALTHCHECK and load balancers.

        Returns ``200 OK`` with JSON body when all components are healthy,
        ``503 Service Unavailable`` when any component is degraded.
        """
        from starlette.responses import JSONResponse

        result = await self._get_health()
        status_code = 200 if result["status"] == "ok" else 503
        return JSONResponse(result, status_code=status_code)

    async def _audit_endpoint(self, request: Request) -> Response:
        """Read-access audit log HTTP endpoint.

        ``GET /audit`` — returns a JSON list of access log entries.

        Query parameters:
            agent_id: Filter entries to this agent (optional).
            after: ISO-8601 timestamp; only entries after this time (optional).
            limit: Max entries to return (default: 100, max: 1000).
            doc_id: Filter entries for a specific document (optional).

        .. note::
            ``agent_id`` in log entries is self-reported by callers and spoofable;
            the audit log is advisory-only and must not be used for access control.

        .. warning:: SECURITY: Trust boundary
            This endpoint is **unauthenticated** and exposes the full agent access
            history to anyone with HTTP access to this server. It is suitable only
            for trusted-network deployments (e.g. localhost or a private LAN). When
            Lithos adds an authentication layer, this endpoint MUST be gated behind
            it.
        """
        from starlette.responses import JSONResponse

        agent_id = request.query_params.get("agent_id")
        after = request.query_params.get("after")
        doc_id = request.query_params.get("doc_id")
        try:
            limit = int(request.query_params.get("limit", "100"))
        except ValueError:
            limit = 100

        # Validate `after` before passing to the coordination layer.
        if after is not None:
            from datetime import datetime as _datetime

            try:
                _datetime.fromisoformat(after.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return JSONResponse(
                    {
                        "error": "invalid_after",
                        "message": f"'after' could not be parsed as a datetime: {after!r}",
                    },
                    status_code=400,
                )

        try:
            entries = await self.coordination.get_audit_log(
                agent_id=agent_id,
                after=after,
                limit=limit,
                doc_id=doc_id,
            )
        except Exception:
            import logging as _logging

            _logging.getLogger(__name__).error(
                "_audit_endpoint: get_audit_log raised unexpectedly", exc_info=True
            )
            return JSONResponse(
                {"error": "audit_log_unavailable", "entries": []},
                status_code=503,
            )
        return JSONResponse(
            {
                "entries": [
                    {
                        "id": e.id,
                        "agent_id": e.agent_id,
                        "doc_id": e.doc_id,
                        "operation": e.operation,
                        "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                    }
                    for e in entries
                ]
            }
        )

    async def _sse_endpoint(self, request: Request) -> Response:
        """Server-Sent Events delivery endpoint.

        Query parameters:
            types: Comma-separated event type filter (e.g. ``note.created,task.completed``).
            tags:  Comma-separated tag filter (any match, e.g. ``research,pricing``).
            since: Replay from a specific event ID in the ring buffer (exclusive).

        Headers:
            Last-Event-ID: Standard SSE reconnect header; takes precedence over ``?since=``.

        Returns ``503`` when SSE is disabled via config and ``429`` when the
        active client limit has been reached.
        """
        sse_config = self._config.events

        if not sse_config.sse_enabled:
            return Response(
                content="SSE delivery is disabled",
                status_code=503,
                media_type="text/plain",
            )

        # Enforce MCP auth boundary on /events (spec requirement).
        # When FastMCP has auth configured, app-level AuthenticationMiddleware
        # populates request.scope["user"] with AuthenticatedUser for valid tokens.
        if self.mcp.auth is not None:
            from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

            if not isinstance(request.scope.get("user"), AuthenticatedUser):
                return Response(
                    content="Authentication required",
                    status_code=401,
                    media_type="text/plain",
                )

        if self._sse_client_count >= sse_config.max_sse_clients:
            return Response(
                content="Too many SSE clients",
                status_code=429,
                media_type="text/plain",
            )

        # Parse filters from query params
        raw_types = request.query_params.get("types")
        event_types: list[str] | None = (
            [t.strip() for t in raw_types.split(",") if t.strip()] if raw_types else None
        )

        raw_tags = request.query_params.get("tags")
        tag_filter: list[str] | None = (
            [t.strip() for t in raw_tags.split(",") if t.strip()] if raw_tags else None
        )

        # Determine replay start: Last-Event-ID header takes precedence over ?since=
        since_id: str | None = request.headers.get("last-event-id") or request.query_params.get(
            "since"
        )

        queue = self.event_bus.subscribe(event_types=event_types, tags=tag_filter)

        # Increment before returning the StreamingResponse to avoid a soft race
        # where concurrent requests all pass the capacity check before any
        # generator starts and increments the counter.
        self._sse_client_count += 1

        async def _event_stream():
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.sse.connect") as conn_span:
                conn_span.set_attribute("lithos.sse.since_id", since_id or "")
                conn_span.set_attribute(
                    "lithos.sse.event_types", ",".join(event_types) if event_types else ""
                )
                try:
                    # Replay buffered events if a since_id was provided
                    if since_id:
                        with tracer.start_as_current_span("lithos.sse.replay") as replay_span:
                            replayed = self.event_bus.get_buffered_since(since_id)
                            replay_count = 0
                            for evt in replayed:
                                # Apply the same filters to replayed events
                                if event_types and evt.type not in event_types:
                                    continue
                                if tag_filter and not any(t in evt.tags for t in tag_filter):
                                    continue
                                replay_count += 1
                                lithos_metrics.sse_events_delivered.add(1)
                                yield _format_sse(evt)
                            replay_span.set_attribute("lithos.sse.replayed", replay_count)

                    # Stream live events
                    while True:
                        try:
                            evt = await asyncio.wait_for(queue.get(), timeout=15.0)
                            lithos_metrics.sse_events_delivered.add(1)
                            yield _format_sse(evt)
                        except asyncio.TimeoutError:
                            # Send keepalive comment to prevent proxy/firewall disconnects
                            yield ": keepalive\n\n"
                        except asyncio.CancelledError:
                            break
                except Exception as exc:
                    conn_span.record_exception(exc)
                    conn_span.set_status(StatusCode.ERROR, str(exc))
                    logger.exception("SSE stream error")
                finally:
                    self._sse_client_count -= 1
                    self.event_bus.unsubscribe(queue)

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    async def initialize(self) -> None:
        """Initialize all components."""
        _init_start = time.perf_counter()
        tracer = get_tracer()
        with tracer.start_as_current_span("lithos.server.initialize") as span:
            span.set_attribute("lithos.server.host", self._config.server.host)
            span.set_attribute("lithos.server.port", self._config.server.port)

            try:
                # Ensure directories exist
                self.config.ensure_directories()

                # Initialize coordination database
                await self.coordination.initialize()

                # Register active claims gauge observer
                register_active_claims_observer(lambda: self._cached_active_claims)

                # Register SSE active clients gauge observer
                register_sse_active_clients_observer(lambda: self._sse_client_count)

                # Register resource-level OTEL gauges
                register_resource_gauges(
                    get_document_count=lambda: self.knowledge.document_count,
                    get_stale_document_count=lambda: self.knowledge.stale_document_count,
                    get_tantivy_document_count=lambda: self._safe_tantivy_count(),
                    get_chroma_chunk_count=lambda: self._safe_chroma_count(),
                    get_graph_node_count=lambda: len(self.graph.graph.nodes),
                    get_graph_edge_count=lambda: len(self.graph.graph.edges),
                    get_agent_count=lambda: self._cached_agent_count,
                )

                # Probe the persisted semantic index out-of-process before any
                # in-process Chroma access. If the store is unreadable,
                # quarantine it and rebuild from source documents.
                semantic_healthy, semantic_backup = self.search.ensure_semantic_backend_healthy()
                if not semantic_healthy:
                    logger.warning(
                        "Semantic search backend remains unavailable after repair attempt: %s",
                        self.search._semantic_store_error,
                    )

                # Load or build indices.
                # Force access to the tantivy property so schema version check runs.
                tantivy_needs_rebuild = self.search.tantivy.needs_rebuild
                if (
                    self.config.index.rebuild_on_start
                    or tantivy_needs_rebuild
                    or semantic_backup is not None
                ):
                    await self._rebuild_indices()
                else:
                    # Try to load cached graph
                    if not self.graph.load_cache():
                        await self._rebuild_indices()

                # Pre-warm the embedding model in the background so the first real
                # request does not block the event loop.  Skip if the rebuild path
                # already loaded it synchronously.
                if self.search.chroma._model is None:
                    task = asyncio.create_task(self._prewarm_embeddings())
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)

            except Exception as exc:
                span.record_exception(exc)
                span.set_status(StatusCode.ERROR, str(exc))
                raise
            finally:
                # Record startup duration whether initialisation succeeded or failed.
                elapsed_ms = (time.perf_counter() - _init_start) * 1000
                lithos_metrics.startup_duration.record(elapsed_ms)
                span.set_attribute("lithos.startup_duration", elapsed_ms)

    def _safe_tantivy_count(self) -> int:
        """Return Tantivy document count, 0 on any error."""
        try:
            return self.search.tantivy.count_docs()
        except Exception:
            return 0

    def _safe_chroma_count(self) -> int:
        """Return ChromaDB chunk count, 0 on any error."""
        try:
            healthy, _ = self.search.ensure_semantic_backend_healthy()
            if not healthy:
                return 0
            return self.search.chroma.count_chunks()
        except Exception:
            return 0

    async def _prewarm_embeddings(self) -> None:
        """Pre-warm the embedding model, logging errors instead of crashing."""
        tracer = get_tracer()
        with tracer.start_as_current_span("lithos.embeddings.prewarm") as span:
            try:
                await self.search.ensure_embeddings_loaded()
                span.set_attribute("lithos.embeddings.status", "ok")
            except Exception:
                span.set_attribute("lithos.embeddings.status", "failed")
                logger.warning("Background embedding model pre-warm failed", exc_info=True)

    async def _rebuild_indices(self) -> None:
        """Rebuild all search indices from files."""
        tracer = get_tracer()
        with tracer.start_as_current_span("lithos.index.rebuild") as span:
            self.search.clear_all()
            self.graph.clear()
            self.knowledge.rescan()

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

    def _bfs_provenance(self, start_id: str, direction: str, depth: int) -> list[dict[str, str]]:
        """BFS traversal over provenance indexes.

        Args:
            start_id: Starting document ID (excluded from results).
            direction: "sources" or "derived".
            depth: Maximum traversal depth (already clamped to 1-3).

        Returns:
            Sorted list of {id, title} dicts for discovered nodes.
        """
        visited: set[str] = {start_id}
        frontier: collections.deque[str] = collections.deque()

        # Seed the frontier with immediate neighbours
        if direction == "sources":
            for nid in self.knowledge.get_doc_sources(start_id):
                if self.knowledge.has_document(nid) and nid not in visited:
                    frontier.append(nid)
                    visited.add(nid)
        else:  # "derived"
            for nid in self.knowledge.get_derived_docs(start_id):
                if nid not in visited:
                    frontier.append(nid)
                    visited.add(nid)

        current_depth = 1
        result_ids: list[str] = list(frontier)

        while current_depth < depth and frontier:
            next_frontier: list[str] = []
            for node_id in frontier:
                if direction == "sources":
                    neighbours = self.knowledge.get_doc_sources(node_id)
                    for nid in neighbours:
                        if self.knowledge.has_document(nid) and nid not in visited:
                            next_frontier.append(nid)
                            visited.add(nid)
                else:
                    neighbours = self.knowledge.get_derived_docs(node_id)
                    for nid in neighbours:
                        if nid not in visited:
                            next_frontier.append(nid)
                            visited.add(nid)
            frontier = collections.deque(next_frontier)
            result_ids.extend(next_frontier)
            current_depth += 1

        # Sort by ID for deterministic output, resolve titles
        return sorted(
            [
                {
                    "id": nid,
                    "title": self.knowledge.get_title_by_id(nid),
                }
                for nid in result_ids
            ],
            key=lambda n: n["id"],
        )

    def start_file_watcher(self) -> None:
        """Start watching for file changes."""
        if self._observer:
            return

        try:
            self._watch_loop = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError("File watcher requires a running asyncio event loop") from None

        handler = _FileChangeHandler(self, self._watch_loop)
        observer = Observer()
        observer.schedule(
            handler,
            str(self.config.storage.knowledge_path),
            recursive=True,
        )
        observer.start()
        self._observer = observer

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

                            lithos_metrics.file_watcher_events.add(1, {"event_type": "deleted"})
                            await self._emit(
                                LithosEvent(
                                    type=NOTE_DELETED,
                                    payload={"path": str(relative_path)},
                                )
                            )
                    else:
                        is_new = not self.knowledge.get_id_by_path(relative_path)
                        doc = await self.knowledge.sync_from_disk(relative_path)
                        self.search.index_document(doc)
                        self.graph.add_document(doc)
                        self.graph.save_cache()

                        event_type = "created" if is_new else "updated"
                        lithos_metrics.file_watcher_events.add(1, {"event_type": event_type})
                        await self._emit(
                            LithosEvent(
                                type=NOTE_UPDATED,
                                payload={"path": str(relative_path)},
                            )
                        )
                except Exception as e:
                    logger.error("Error handling file change %s: %s", path, e)

    def _register_tools(self) -> None:
        """Register all MCP tools."""

        # ==================== Knowledge Tools ====================

        @self.mcp.tool()
        @tool_metrics()
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
            derived_from_ids: list[str] | None = None,
            ttl_hours: float | None = None,
            expires_at: str | None = None,
            expected_version: int | None = None,
        ) -> dict[str, Any]:
            """Create or update a knowledge file.

            Args:
                title: Title of the knowledge item
                content: Markdown content (without frontmatter)
                agent: Your agent identifier
                tags: List of tags. On update: null/omit preserves existing; [] clears
                    all tags; non-empty list replaces.
                confidence: Confidence score 0-1 (default: 1.0 on create). On update:
                    null/omit preserves existing; float sets new value.
                path: Subdirectory path (e.g., "procedures")
                id: UUID to update existing; omit to create new
                source_task: Task ID this knowledge came from
                source_url: URL provenance for this knowledge. On update: null/omit
                    preserves existing; "" clears; string sets new value.
                derived_from_ids: List of source document UUIDs this note was derived
                    from. On update: null/omit preserves existing; [] clears;
                    non-empty list replaces.
                ttl_hours: Time-to-live in hours from now. Computes expires_at.
                    Mutually exclusive with expires_at.
                expires_at: Absolute ISO 8601 expiry datetime. On update: null/omit
                    preserves existing; "" clears; ISO string sets new value.
                    Mutually exclusive with ttl_hours.
                expected_version: If provided on update, reject with version_conflict if the
                    document's current version differs. Omit to skip version checking.
                    On create, this parameter is silently ignored.

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

                if len(content.encode("utf-8")) > self._config.storage.max_content_size_bytes:
                    return {
                        "status": "error",
                        "code": "content_too_large",
                        "message": f"Content exceeds maximum size of {self._config.storage.max_content_size_bytes} bytes",
                        "warnings": [],
                    }

                warnings: list[str] = []

                # Validate ttl_hours / expires_at mutual exclusion
                if ttl_hours is not None and expires_at is not None:
                    return {
                        "status": "error",
                        "code": "invalid_input",
                        "message": "Provide either ttl_hours or expires_at, not both.",
                        "warnings": [],
                    }

                # Validate ttl_hours
                if ttl_hours is not None and (
                    not isinstance(ttl_hours, (int, float))
                    or math.isnan(ttl_hours)
                    or math.isinf(ttl_hours)
                    or ttl_hours <= 0
                ):
                    return {
                        "status": "error",
                        "code": "invalid_input",
                        "message": "ttl_hours must be a finite positive number.",
                        "warnings": [],
                    }

                # Emit freshness span attributes
                if ttl_hours is not None:
                    span.set_attribute("freshness.ttl_hours", ttl_hours)
                elif expires_at is not None and expires_at != "":
                    span.set_attribute("freshness.expires_at_set", True)

                # Compute expires_at_dt from ttl_hours or expires_at string
                expires_at_dt: datetime | None | _UnsetType
                if ttl_hours is not None:
                    expires_at_dt = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
                elif id is not None:
                    # Update path: map MCP boundary to manager semantics
                    # None (omitted) → _UNSET (preserve), "" → None (clear), str → parse
                    if expires_at is None:
                        expires_at_dt = _UNSET
                    elif expires_at == "":
                        expires_at_dt = None
                    else:
                        try:
                            expires_at_dt = datetime.fromisoformat(expires_at)
                            if expires_at_dt.tzinfo is None:
                                expires_at_dt = expires_at_dt.replace(tzinfo=timezone.utc)
                            else:
                                expires_at_dt = expires_at_dt.astimezone(timezone.utc)
                        except ValueError:
                            return {
                                "status": "error",
                                "code": "invalid_input",
                                "message": f"Invalid expires_at datetime: {expires_at}",
                                "warnings": [],
                            }
                else:
                    # Create path: None means no expiry, str → parse
                    if expires_at is None:
                        expires_at_dt = None
                    else:
                        try:
                            expires_at_dt = datetime.fromisoformat(expires_at)
                            if expires_at_dt.tzinfo is None:
                                expires_at_dt = expires_at_dt.replace(tzinfo=timezone.utc)
                            else:
                                expires_at_dt = expires_at_dt.astimezone(timezone.utc)
                        except ValueError:
                            return {
                                "status": "error",
                                "code": "invalid_input",
                                "message": f"Invalid expires_at datetime: {expires_at}",
                                "warnings": [],
                            }

                try:
                    if id:
                        # Update existing — map MCP boundary to manager semantics:
                        # source_url: None (omitted) → _UNSET (preserve), "" → None (clear),
                        #             str → pass through
                        url_arg: str | None | _UnsetType
                        if source_url is None:
                            url_arg = _UNSET
                        elif source_url == "":
                            url_arg = None
                        else:
                            url_arg = source_url

                        # derived_from_ids: None (omitted) → _UNSET (preserve),
                        #                   [] → [] (clear), non-empty → pass through
                        prov_arg: list[str] | None | _UnsetType = (
                            _UNSET if derived_from_ids is None else derived_from_ids
                        )

                        # tags: None (omitted) → _UNSET (preserve), [] → clear, list → set
                        tags_arg: list[str] | _UnsetType = _UNSET if tags is None else tags

                        # confidence: None (omitted) → _UNSET (preserve), float → set
                        conf_arg: float | _UnsetType = _UNSET if confidence is None else confidence

                        result = await self.knowledge.update(
                            id=id,
                            agent=agent,
                            title=title,
                            content=content,
                            tags=tags_arg,
                            confidence=conf_arg,
                            source_url=url_arg,
                            derived_from_ids=prov_arg,
                            expires_at=expires_at_dt,
                            expected_version=expected_version,
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
                            derived_from_ids=derived_from_ids,
                            expires_at=expires_at_dt,  # type: ignore[arg-type]
                        )
                except SlugCollisionError as exc:
                    span.set_attribute("lithos.write_status", "error")
                    return {
                        "status": "error",
                        "code": "slug_collision",
                        "message": str(exc),
                        "warnings": [],
                    }

                # Handle non-success results via WriteResult fields
                if result.status == "duplicate":
                    span.set_attribute("lithos.write_status", "duplicate")
                    dup = result.duplicate_of
                    return {
                        "status": "duplicate",
                        "duplicate_of": {
                            "id": dup.id,
                            "title": dup.title,
                            "source_url": dup.source_url,
                        }
                        if dup
                        else None,
                        "message": result.message,
                        "warnings": warnings + result.warnings,
                    }
                elif result.status == "error":
                    span.set_attribute("lithos.write_status", "error")
                    error_response: dict[str, Any] = {
                        "status": "error",
                        "code": result.error_code,
                        "message": result.message,
                        "warnings": warnings + result.warnings,
                    }
                    if result.current_version is not None:
                        error_response["current_version"] = result.current_version
                    return error_response

                doc = result.document
                assert doc is not None
                warnings.extend(result.warnings)

                # Update indices
                self.search.index_document(doc)
                self.graph.add_document(doc)
                self.graph.save_cache()

                span.set_attribute("lithos.doc_id", doc.id)
                span.set_attribute("lithos.write_status", result.status)
                span.set_attribute(
                    "lithos.provenance.source_count",
                    len(doc.metadata.derived_from_ids),
                )
                if result.warnings:
                    span.set_attribute("lithos.provenance.warning_count", len(result.warnings))
                logger.info("lithos_write completed doc_id=%s status=%s", doc.id, result.status)

                await self._emit(
                    LithosEvent(
                        type=NOTE_UPDATED if id else NOTE_CREATED,
                        agent=agent,
                        payload={"id": doc.id, "title": doc.title, "path": str(doc.path)},
                        tags=doc.metadata.tags,
                    )
                )

                # Design note: knowledge.update() acquires _write_lock *before* reading
                # the doc, checking expected_version, and writing. The read, version
                # check, and write all happen inside the same lock acquisition, so
                # there is no TOCTOU window — concurrent writers are fully serialised.
                return {
                    "status": result.status,
                    "id": doc.id,
                    "path": str(doc.path),
                    "version": doc.metadata.version,
                    "warnings": warnings,
                }

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_read(
            id: str | None = None,
            path: str | None = None,
            max_length: int | None = None,
            agent_id: str | None = None,
        ) -> dict[str, Any]:
            """Read a knowledge file by ID or path.

            Args:
                id: UUID of knowledge item
                path: File path relative to knowledge/
                max_length: Truncate content to N characters
                agent_id: Caller identity for audit logging (optional)

            Returns:
                Dict with id, title, content, metadata, links, truncated,
                retrieval_count
            """
            logger.info("lithos_read id=%s path=%s", id, path)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.read") as span:
                span.set_attribute("lithos.tool", "lithos_read")
                if id:
                    span.set_attribute("lithos.id", id)

                try:
                    doc, truncated = await self.knowledge.read(
                        id=id,
                        path=path,
                        max_length=max_length,
                    )
                except FileNotFoundError as e:
                    return {
                        "status": "error",
                        "code": "doc_not_found",
                        "message": str(e),
                    }

                # Audit log — awaited so the write is committed before we query
                # retrieval_count (avoids TOCTOU off-by-one). lithos_search uses
                # fire-and-forget (asyncio.create_task) for its batch write since
                # retrieval_count accuracy is not required there.
                audit_agent = agent_id or "unknown"
                await self.coordination.log_access(
                    doc_id=doc.id,
                    operation="read",
                    agent_id=audit_agent,
                )

                # Retrieval count — how many times this doc has been read
                retrieval_count = await self.coordination.get_retrieval_count(doc.id)

                span.set_attribute("lithos.truncated", truncated)
                meta = doc.metadata.to_dict()
                meta["source_url"] = doc.metadata.source_url  # null when None
                meta.setdefault("derived_from_ids", [])
                return {
                    "id": doc.id,
                    "title": doc.title,
                    "content": doc.content,
                    "metadata": meta,
                    "links": [
                        {"target": link.target, "display": link.display} for link in doc.links
                    ],
                    "truncated": truncated,
                    "retrieval_count": retrieval_count,
                }

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_delete(
            id: str,
            agent: str,
        ) -> dict[str, Any]:
            """Delete a knowledge file.

            Args:
                id: UUID of knowledge item to delete
                agent: Agent performing deletion (required for audit trail)

            Returns:
                Dict with success boolean, or error envelope if document not found
            """
            logger.info("lithos_delete id=%s agent=%s", id, agent)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.delete") as span:
                span.set_attribute("lithos.tool", "lithos_delete")
                span.set_attribute("lithos.id", id)
                span.set_attribute("lithos.agent", agent)
                await self.coordination.ensure_agent_known(agent)

                success, path = await self.knowledge.delete(id)

                if not success:
                    return {
                        "status": "error",
                        "code": "doc_not_found",
                        "message": f"Document not found: {id}",
                    }

                self.search.remove_document(id)
                self.graph.remove_document(id)
                self.graph.save_cache()

                await self._emit(
                    LithosEvent(
                        type=NOTE_DELETED,
                        agent=agent,
                        payload={"id": id, "path": path},
                    )
                )

                return {"success": True}

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_search(
            query: str,
            limit: int = 10,
            mode: str = "hybrid",
            tags: list[str] | None = None,
            author: str | None = None,
            path_prefix: str | None = None,
            threshold: float | None = None,
            seed_ids: list[str] | None = None,
            graph_depth: int = 2,
            agent_id: str | None = None,
        ) -> dict[str, Any]:
            """Search across the knowledge base.

            Supports four search modes:
            - ``hybrid`` (default): Merges Tantivy BM25 full-text and ChromaDB
              cosine-similarity results using Reciprocal Rank Fusion (RRF, k=60).
              Best overall quality.
            - ``fulltext``: Pure Tantivy full-text search (BM25). Supports Tantivy
              query syntax (e.g. field-specific queries, boolean operators).
            - ``semantic``: Pure ChromaDB semantic/vector search. Finds documents
              with similar meaning even when keywords differ.
            - ``graph``: Wiki-link graph traversal. Discovers related documents by
              following links from seed documents up to *graph_depth* hops.
              Seeds are either provided via *seed_ids* or discovered automatically
              via a fast hybrid search on *query*.

            Args:
                query: Search query string
                limit: Max results (default: 10)
                mode: Search mode — "hybrid" | "fulltext" | "semantic" | "graph"
                      (default: "hybrid")
                tags: Filter by tags (AND) — fulltext/semantic/hybrid only
                author: Filter by author (fulltext/semantic/hybrid only)
                path_prefix: Filter by path prefix (fulltext/semantic/hybrid only)
                threshold: Minimum similarity 0-1 for semantic/hybrid (default: 0.5)
                seed_ids: Starting document IDs for graph mode.  If omitted,
                          seeds are discovered via hybrid search.
                graph_depth: BFS hop depth for graph mode (1-3, default: 2)
                agent_id: Caller identity for audit logging (optional)

            Returns:
                Dict with results list containing id, title, snippet, score, path,
                source_url, updated_at, is_stale, derived_from_ids
            """
            logger.info("lithos_search mode=%s query_len=%d limit=%d", mode, len(query), limit)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.search") as span:
                span.set_attribute("lithos.tool", f"lithos_search:{mode}")
                span.set_attribute("lithos.query.length", len(query))
                span.set_attribute(
                    "lithos.query.sha256",
                    hashlib.sha256(query.encode()).hexdigest()[:16],
                )
                span.set_attribute("lithos.limit", limit)
                span.set_attribute("lithos.mode", mode)

                valid_modes = {"hybrid", "fulltext", "semantic", "graph"}
                if mode not in valid_modes:
                    return {
                        "status": "error",
                        "code": "invalid_mode",
                        "message": f"Unknown search mode {mode!r}. Valid values: hybrid, fulltext, semantic, graph.",
                    }

                def _build_result(r: Any, score_attr: str = "score") -> dict[str, Any]:
                    return {
                        "id": r.id,
                        "title": r.title,
                        "snippet": r.snippet,
                        "score": getattr(r, score_attr),
                        "path": r.path,
                        "source_url": r.source_url,
                        "updated_at": r.updated_at,
                        "is_stale": r.is_stale,
                        "derived_from_ids": self.knowledge.get_doc_sources(r.id),
                    }

                # Thread safety note: SearchManager read methods (full_text_search, semantic_search,
                # hybrid_search) are wrapped in asyncio.to_thread() to avoid blocking the event loop.
                # Concurrent reads via tantivy-py and ChromaDB are safe. Known risk: lithos_write
                # calls index_document() synchronously without to_thread() — concurrent read+write
                # is not protected by a lock. This is an existing limitation pre-LCMA; tracked for
                # future hardening. ChromaDB model init race on ensure_embeddings_loaded() is
                # mitigated by the existing warmup call at server startup.
                if mode == "fulltext":
                    ft_results = await asyncio.to_thread(
                        self.search.full_text_search,
                        query=query,
                        limit=limit,
                        tags=tags,
                        author=author,
                        path_prefix=path_prefix,
                    )
                    results_payload = [_build_result(r) for r in ft_results]
                elif mode == "semantic":
                    sem_results = await asyncio.to_thread(
                        self.search.semantic_search,
                        query=query,
                        limit=limit,
                        threshold=threshold,
                        tags=tags,
                        author=author,
                        path_prefix=path_prefix,
                    )
                    results_payload = [
                        _build_result(r, score_attr="similarity") for r in sem_results
                    ]
                elif mode == "graph":
                    graph_results = await asyncio.to_thread(
                        self.search.graph_search,
                        query=query,
                        graph=self.graph,
                        seed_ids=seed_ids,
                        depth=graph_depth,
                        limit=limit,
                        tags=tags,
                        author=author,
                        path_prefix=path_prefix,
                        threshold=threshold,
                    )
                    results_payload = [_build_result(r) for r in graph_results]
                else:
                    # hybrid (default)
                    hybrid_results = await asyncio.to_thread(
                        self.search.hybrid_search,
                        query=query,
                        limit=limit,
                        threshold=threshold,
                        tags=tags,
                        author=author,
                        path_prefix=path_prefix,
                    )
                    results_payload = [_build_result(r) for r in hybrid_results]

                span.set_attribute("lithos.result_count", len(results_payload))
                logger.info("lithos_search mode=%s results=%d", mode, len(results_payload))

                # Audit log every returned document in a single batch write — fire-and-forget.
                # Using log_access_batch avoids N concurrent SQLite connections (previously
                # one asyncio.create_task per result document).
                audit_agent = agent_id or "unknown"
                asyncio.create_task(  # noqa: RUF006
                    self.coordination.log_access_batch(
                        doc_ids=[r["id"] for r in results_payload],
                        operation="search_result",
                        agent_id=audit_agent,
                    )
                )

                return {"results": results_payload}

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_cache_lookup(
            query: str,
            source_url: str | None = None,
            max_age_hours: float | None = None,
            min_confidence: float = 0.5,
            limit: int = 3,
            tags: list[str] | None = None,
        ) -> dict[str, Any]:
            """Check if fresh cached knowledge exists before doing expensive research.

            Returns a cache hit with full document content if fresh knowledge exists,
            a stale reference if expired knowledge exists (update instead of duplicate),
            or a clean miss if nothing relevant is found.

            Args:
                query: What you are about to research
                source_url: Canonical URL for exact dedup-aware lookup
                max_age_hours: Reject docs older than N hours (uses updated_at)
                min_confidence: Minimum confidence score threshold — candidates whose
                    ``metadata.confidence`` is strictly below this value are skipped
                    entirely (default: 0.5).
                limit: Max candidate docs to evaluate (default: 3).
                tags: Restrict to tagged docs (AND semantics)

            Returns:
                Dict with hit, document, stale_exists, stale_id
            """
            logger.info("lithos_cache_lookup query_len=%d source_url=%s", len(query), source_url)

            # Input validation
            if max_age_hours is not None and max_age_hours <= 0:
                return {
                    "status": "error",
                    "code": "invalid_input",
                    "message": "max_age_hours must be positive.",
                }
            if limit < 1:
                return {
                    "status": "error",
                    "code": "invalid_input",
                    "message": "limit must be >= 1.",
                }
            if not (0.0 <= min_confidence <= 1.0):
                return {
                    "status": "error",
                    "code": "invalid_input",
                    "message": "min_confidence must be between 0.0 and 1.0.",
                }

            _lookup_start = time.perf_counter()
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.cache_lookup") as span:
                span.set_attribute("lithos.tool", "lithos_cache_lookup")
                span.set_attribute("cache.source_url_used", source_url is not None)

                candidates: list[str] = []
                candidates_evaluated = 0

                # Fast path: source_url exact lookup
                if source_url is not None:
                    fast_doc = await self.knowledge.find_by_source_url(source_url)
                    if fast_doc is not None:
                        # Tag filtering on fast path
                        if tags:
                            doc_tags = fast_doc.metadata.tags
                            if all(t in doc_tags for t in tags):
                                candidates = [fast_doc.id]
                            # else: tag filter failed, fall through to semantic
                        else:
                            candidates = [fast_doc.id]

                # Fallback: semantic search
                if not candidates:
                    try:
                        sem_results = await asyncio.to_thread(
                            self.search.semantic_search,
                            query=query,
                            limit=limit,
                            threshold=0.0,
                            tags=tags,
                        )
                        candidates = [r.id for r in sem_results[:limit]]
                    except SearchBackendError as exc:
                        span.set_attribute("cache.search_error", True)
                        elapsed_ms = (time.perf_counter() - _lookup_start) * 1000
                        lithos_metrics.cache_lookup_duration.record(elapsed_ms)
                        lithos_metrics.cache_lookups.add(1, {"outcome": "error_search_backend"})
                        return {
                            "status": "error",
                            "code": "search_backend_error",
                            "message": f"Semantic search backend failed: {exc}",
                        }

                # Evaluate candidates
                best_hit = None
                first_stale_id: str | None = None
                now = datetime.now(timezone.utc)
                passing_docs: list[Any] = []

                for doc_id in candidates:
                    try:
                        doc, _ = await self.knowledge.read(id=doc_id)
                    except (FileNotFoundError, ValueError):
                        continue

                    candidates_evaluated += 1
                    meta = doc.metadata

                    # Skip if below confidence threshold
                    if meta.confidence < min_confidence:
                        continue

                    # Check staleness (explicit expiry)
                    if meta.is_stale:
                        if first_stale_id is None:
                            first_stale_id = doc_id
                        continue

                    # Check max_age_hours
                    if max_age_hours is not None:
                        from lithos.knowledge import _normalize_datetime

                        updated = _normalize_datetime(meta.updated_at)
                        cutoff = now - timedelta(hours=max_age_hours)
                        if updated < cutoff:
                            if first_stale_id is None:
                                first_stale_id = doc_id
                            continue

                    passing_docs.append(doc)

                if passing_docs:
                    best_hit = max(passing_docs, key=lambda d: d.metadata.confidence)

                span.set_attribute("cache.candidates_evaluated", candidates_evaluated)

                elapsed_ms = (time.perf_counter() - _lookup_start) * 1000
                lithos_metrics.cache_lookup_duration.record(elapsed_ms)

                if best_hit is not None:
                    span.set_attribute("cache.hit", True)
                    span.set_attribute("cache.stale_exists", False)
                    lithos_metrics.cache_lookups.add(1, {"outcome": "hit"})
                    return {
                        "hit": True,
                        "document": {
                            "id": best_hit.id,
                            "title": best_hit.title,
                            "content": best_hit.content,
                            "confidence": best_hit.metadata.confidence,
                            "updated_at": best_hit.metadata.updated_at.isoformat(),
                            "expires_at": (
                                best_hit.metadata.expires_at.isoformat()
                                if best_hit.metadata.expires_at
                                else None
                            ),
                            "tags": best_hit.metadata.tags,
                            "source_url": best_hit.metadata.source_url,
                        },
                        "stale_exists": False,
                        "stale_id": None,
                    }
                elif first_stale_id is not None:
                    span.set_attribute("cache.hit", False)
                    span.set_attribute("cache.stale_exists", True)
                    lithos_metrics.cache_lookups.add(1, {"outcome": "miss_stale"})
                    return {
                        "hit": False,
                        "document": None,
                        "stale_exists": True,
                        "stale_id": first_stale_id,
                    }
                else:
                    span.set_attribute("cache.hit", False)
                    span.set_attribute("cache.stale_exists", False)
                    lithos_metrics.cache_lookups.add(1, {"outcome": "miss_clean"})
                    return {
                        "hit": False,
                        "document": None,
                        "stale_exists": False,
                        "stale_id": None,
                    }

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_list(
            path_prefix: str | None = None,
            tags: list[str] | None = None,
            author: str | None = None,
            since: str | None = None,
            limit: int = 50,
            offset: int = 0,
            title_contains: str | None = None,
            content_query: str | None = None,
        ) -> dict[str, Any]:
            """List knowledge documents with filters.

            Args:
                path_prefix: Filter by path prefix
                tags: Filter by tags (AND)
                author: Filter by author
                since: Filter by updated since (ISO datetime)
                limit: Max results (default: 50)
                offset: Pagination offset
                title_contains: Filter by case-insensitive substring match on title
                content_query: Filter by full-text search query (Tantivy). When
                    provided the entire base-filtered set is searched in-memory,
                    so results and ``total`` are always correct across pages.
                    full_text_search is called with a limit equal to the total
                    number of base-filtered documents so no matches are silently
                    dropped.

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

                if title_contains is not None or content_query is not None:
                    # When post-fetch filters are active we must fetch the full
                    # base-filtered set first; filtering after pagination would
                    # miss docs on earlier pages and produce wrong totals.
                    # Step 1: get total count with limit=0 (returns count, no docs).
                    _, total_base = await self.knowledge.list_all(
                        path_prefix=path_prefix,
                        tags=tags,
                        author=author,
                        since=since_dt,
                        limit=0,
                        offset=0,
                    )
                    # Step 2: fetch all base-filtered docs for in-memory filtering.
                    if total_base > 0:
                        all_docs, _ = await self.knowledge.list_all(
                            path_prefix=path_prefix,
                            tags=tags,
                            author=author,
                            since=since_dt,
                            limit=total_base,
                            offset=0,
                        )
                    else:
                        all_docs = []

                    if title_contains is not None:
                        all_docs = [
                            d for d in all_docs if title_contains.lower() in d.title.lower()
                        ]

                    if content_query is not None:
                        try:
                            fts_results = await asyncio.to_thread(
                                self.search.full_text_search,
                                query=content_query,
                                # Use total_base as the cap so we never silently
                                # truncate matches from the base-filtered set.
                                limit=max(total_base, 1),
                            )
                            fts_ids = {r.id for r in fts_results}
                            all_docs = [d for d in all_docs if d.id in fts_ids]
                        except SearchBackendError as exc:
                            return {
                                "status": "error",
                                "code": "search_backend_error",
                                "message": f"Full-text search failed: {exc}",
                            }

                    total = len(all_docs)
                    docs = all_docs[offset : offset + limit]
                else:
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
                            "derived_from_ids": self.knowledge.get_doc_sources(d.id),
                        }
                        for d in docs
                    ],
                    "total": total,
                }

        # ==================== Graph Tools ====================

        @self.mcp.tool()
        @tool_metrics()
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
        @tool_metrics()
        async def lithos_provenance(
            id: str,
            direction: str = "both",
            depth: int = 1,
            include_unresolved: bool = True,
        ) -> dict[str, Any]:
            """Query document lineage via provenance indexes.

            Args:
                id: Document UUID
                direction: "sources", "derived", or "both"
                depth: BFS traversal depth 1-3 (default: 1)
                include_unresolved: Include unresolved source UUIDs (default: True)

            Returns:
                Dict with id, sources, derived, and optionally unresolved_sources
            """
            logger.info("lithos_provenance id=%s direction=%s depth=%d", id, direction, depth)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.provenance") as span:
                span.set_attribute("lithos.tool", "lithos_provenance")
                span.set_attribute("lithos.id", id)
                span.set_attribute("lithos.direction", direction)
                span.set_attribute("lithos.depth", depth)

                if not self.knowledge.has_document(id):
                    return {
                        "status": "error",
                        "code": "doc_not_found",
                        "message": f"Document not found: {id}",
                    }

                if direction not in ("sources", "derived", "both"):
                    direction = "both"

                depth = min(max(depth, 1), 3)

                sources: list[dict[str, str]] = []
                derived: list[dict[str, str]] = []

                if direction in ("sources", "both"):
                    sources = self._bfs_provenance(id, "sources", depth)
                if direction in ("derived", "both"):
                    derived = self._bfs_provenance(id, "derived", depth)

                result: dict[str, Any] = {
                    "id": id,
                    "sources": sources,
                    "derived": derived,
                }

                if include_unresolved:
                    result["unresolved_sources"] = sorted(self.knowledge.get_unresolved_sources(id))

                span.set_attribute("lithos.sources_count", len(sources))
                span.set_attribute("lithos.derived_count", len(derived))
                return result

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_tags(
            prefix: str | None = None,
        ) -> dict[str, dict[str, int]]:
            """Get all tags with document counts.

            Args:
                prefix: Optional prefix filter (case-insensitive). Only tags starting with this prefix are returned.

            Returns:
                Dict with tags mapping tag name to count
            """
            logger.info("lithos_tags prefix=%s", prefix)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.tags") as span:
                span.set_attribute("lithos.tool", "lithos_tags")
                tags = await self.knowledge.get_all_tags()
                if prefix is not None:
                    tags = {k: v for k, v in tags.items() if k.lower().startswith(prefix.lower())}
                span.set_attribute("lithos.tag_count", len(tags))
                return {"tags": tags}

        # ==================== Agent Tools ====================

        @self.mcp.tool()
        @tool_metrics()
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

                if success:
                    await self._emit(
                        LithosEvent(
                            type=AGENT_REGISTERED,
                            agent=id,
                            payload={"agent_id": id, "name": name or ""},
                        )
                    )

                return {"success": success, "created": created}

        @self.mcp.tool()
        @tool_metrics()
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
        @tool_metrics()
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
        @tool_metrics()
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

                await self._emit(
                    LithosEvent(
                        type=TASK_CREATED,
                        agent=agent,
                        payload={"task_id": task_id, "title": title},
                    )
                )

                return {"task_id": task_id}

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_task_update(
            task_id: str,
            agent: str,
            title: str | None = None,
            description: str | None = None,
            tags: list[str] | None = None,
        ) -> dict[str, Any]:
            """Update mutable task metadata (title, description, tags).

            At least one of title, description, or tags must be provided.

            Args:
                task_id: Task ID to update
                agent: Agent making the update
                title: New task title (optional)
                description: New task description (optional)
                tags: New task tags (optional)

            Returns:
                Dict with success and message
            """
            if title is None and description is None and tags is None:
                return {
                    "status": "error",
                    "code": "invalid_input",
                    "message": "At least one of title, description, or tags must be provided",
                }

            logger.info("lithos_task_update task=%s agent=%s", task_id, agent)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_update") as span:
                span.set_attribute("lithos.tool", "lithos_task_update")
                span.set_attribute("lithos.agent", agent)
                span.set_attribute("lithos.task_id", task_id)
                updated = await self.coordination.update_task(
                    task_id=task_id,
                    agent=agent,
                    title=title,
                    description=description,
                    tags=tags,
                )
                span.set_attribute("lithos.success", updated)

                if updated:
                    return {"success": True, "message": f"Task {task_id} updated"}
                return {
                    "status": "error",
                    "code": "task_not_found",
                    "message": f"Task {task_id} not found",
                }

        @self.mcp.tool()
        @tool_metrics()
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

                if not success:
                    return {
                        "status": "error",
                        "code": "claim_failed",
                        "message": (
                            f"Could not claim aspect '{aspect}' on task '{task_id}': "
                            "task not found, not open, or aspect already claimed by another agent."
                        ),
                    }

                await self._emit(
                    LithosEvent(
                        type=TASK_CLAIMED,
                        agent=agent,
                        payload={"task_id": task_id, "agent": agent, "aspect": aspect},
                    )
                )

                return {
                    "success": True,
                    "expires_at": expires_at.isoformat(),  # type: ignore[union-attr]
                }

        @self.mcp.tool()
        @tool_metrics()
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

                if not success:
                    return {
                        "status": "error",
                        "code": "claim_not_found",
                        "message": (
                            f"No active claim found for task '{task_id}', "
                            f"aspect '{aspect}', agent '{agent}'."
                        ),
                    }

                return {
                    "success": True,
                    "new_expires_at": new_expires.isoformat(),  # type: ignore[union-attr]
                }

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_task_release(
            task_id: str,
            aspect: str,
            agent: str,
        ) -> dict[str, Any]:
            """Release a claim.

            Args:
                task_id: Task ID
                aspect: Claimed aspect
                agent: Agent releasing the claim

            Returns:
                Dict with success boolean, or error envelope if no matching claim
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

                if not success:
                    return {
                        "status": "error",
                        "code": "claim_not_found",
                        "message": (
                            f"No matching claim found for task '{task_id}', "
                            f"aspect '{aspect}', agent '{agent}'."
                        ),
                    }

                await self._emit(
                    LithosEvent(
                        type=TASK_RELEASED,
                        agent=agent,
                        payload={"task_id": task_id, "agent": agent, "aspect": aspect},
                    )
                )

                return {"success": True}

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_task_complete(
            task_id: str,
            agent: str,
        ) -> dict[str, Any]:
            """Mark a task as completed.

            Args:
                task_id: Task ID
                agent: Agent completing the task

            Returns:
                Dict with success boolean, or error envelope if task not found or not open
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

                if not success:
                    return {
                        "status": "error",
                        "code": "task_not_found",
                        "message": f"Task '{task_id}' not found or not in an open state.",
                    }

                await self._emit(
                    LithosEvent(
                        type=TASK_COMPLETED,
                        agent=agent,
                        payload={"task_id": task_id, "agent": agent},
                    )
                )

                return {"success": True}

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_task_cancel(
            task_id: str,
            agent: str,
            reason: str | None = None,
        ) -> dict[str, Any]:
            """Cancel a task, releasing all claims.

            Args:
                task_id: Task ID
                agent: Agent cancelling the task
                reason: Optional reason for cancellation

            Returns:
                Dict with success boolean
            """
            logger.info("lithos_task_cancel task=%s agent=%s reason=%s", task_id, agent, reason)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_cancel") as span:
                span.set_attribute("lithos.tool", "lithos_task_cancel")
                span.set_attribute("lithos.agent", agent)
                span.set_attribute("lithos.task_id", task_id)
                success = await self.coordination.cancel_task(
                    task_id=task_id,
                    agent=agent,
                    reason=reason,
                )
                span.set_attribute("lithos.success", success)

                if success:
                    await self._emit(
                        LithosEvent(
                            type=TASK_CANCELLED,
                            agent=agent,
                            payload={"task_id": task_id, "agent": agent, "reason": reason},
                        )
                    )
                    return {"success": True}

                return {
                    "status": "error",
                    "code": "task_not_found",
                    "message": f"Task {task_id} not found or already closed",
                }

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_task_list(
            agent: str | None = None,
            status: str | None = None,
            tags: list[str] | None = None,
            since: str | None = None,
        ) -> dict[str, list[dict[str, Any]]]:
            """List tasks with optional filters.

            Args:
                agent: Filter by creating agent
                status: Filter by status: "open", "completed", or "cancelled" (None = all)
                tags: Filter by tags (task must have all specified tags)
                since: Filter by created_at >= this ISO datetime string (e.g. "2024-01-01T00:00:00Z")

            Returns:
                Dict with tasks list containing id, title, description, status, created_by, created_at, tags
            """
            logger.info(
                "lithos_task_list agent=%s status=%s tags=%s since=%s", agent, status, tags, since
            )
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_list") as span:
                span.set_attribute("lithos.tool", "lithos_task_list")
                if agent:
                    span.set_attribute("lithos.agent", agent)
                if status:
                    span.set_attribute("lithos.status", status)

                tasks = await self.coordination.list_tasks(
                    agent=agent,
                    status=status,
                    tags=tags,
                    since=since,
                )
                return {"tasks": tasks}

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_task_status(
            task_id: str,
        ) -> dict[str, list[dict[str, Any]]]:
            """Get status of a specific task with its active claims.

            Args:
                task_id: Task ID to look up

            Returns:
                Dict with tasks list containing id, title, status, claims
            """
            logger.info("lithos_task_status task_id=%s", task_id)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_status") as span:
                span.set_attribute("lithos.tool", "lithos_task_status")
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
        @tool_metrics()
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

                await self._emit(
                    LithosEvent(
                        type=FINDING_POSTED,
                        agent=agent,
                        payload={
                            "finding_id": finding_id,
                            "task_id": task_id,
                            "agent": agent,
                        },
                    )
                )

                return {"finding_id": finding_id}

        @self.mcp.tool()
        @tool_metrics()
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
        @tool_metrics()
        async def lithos_stats() -> dict[str, Any]:
            """Get knowledge base statistics and health indicators.

            Returns:
                Dict with document counts, search index stats, coordination
                stats, and health signals (index drift, broken links, expired
                claims, last-updated timestamps).
            """
            logger.info("lithos_stats")
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.stats") as span:
                span.set_attribute("lithos.tool", "lithos_stats")

                # Get document count (from in-memory cache — always available)
                total_docs = self.knowledge.document_count

                # Get search stats
                search_stats = self.search.get_stats()
                chroma_chunk_count: int = search_stats.get("chroma_chunk_count", 0)

                # Tantivy document count with None-sentinel on failure.
                # NOTE: this deliberately diverges from _safe_tantivy_count(),
                # which returns *0* on error (used by OTEL gauge probes where a
                # numeric value is always required).  Here we return *None* so
                # callers can distinguish "index not yet written / unavailable"
                # from "zero documents indexed" — a meaningful difference for
                # drift detection and health reporting.  If you need the 0-on-
                # error behaviour, use _safe_tantivy_count() instead.
                tantivy_doc_count: int | None
                try:
                    tantivy_doc_count = self.search.tantivy.count_docs()
                except Exception:
                    tantivy_doc_count = None

                # Index drift: knowledge corpus vs Tantivy index
                index_drift_detected = (
                    tantivy_doc_count is not None and tantivy_doc_count != total_docs
                )

                # Get coordination stats
                coord_stats = await self.coordination.get_stats()

                # Update cached fields for synchronous OTEL gauge callbacks
                self._cached_active_claims = coord_stats.get("open_claims", 0)
                self._cached_agent_count = coord_stats.get("agents", 0)

                # Get tag count
                tags = await self.knowledge.get_all_tags()

                # Unresolved wiki-links: nodes in the graph that have no
                # matching document (represented as __unresolved__ placeholders)
                graph_stats = self.graph.get_stats()
                unresolved_links: int = graph_stats.get("unresolved_links", 0)

                # Stale (expired) documents
                expired_docs = self.knowledge.stale_document_count

                # Index last-updated timestamps from filesystem mtime
                def _dir_mtime(path: Path) -> str | None:
                    try:
                        mtime = path.stat().st_mtime
                        return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
                    except OSError:
                        return None

                tantivy_last_updated = _dir_mtime(self._config.storage.tantivy_path)
                chroma_last_updated = _dir_mtime(self._config.storage.chroma_path)

                return {
                    # Core counts
                    "documents": total_docs,
                    "chroma_chunk_count": chroma_chunk_count,
                    "agents": coord_stats.get("agents", 0),
                    "active_tasks": coord_stats.get("active_tasks", 0),
                    "open_claims": coord_stats.get("open_claims", 0),
                    "tags": len(tags),
                    "duplicate_urls": self.knowledge.duplicate_url_count,
                    # Health indicators
                    "index_drift_detected": index_drift_detected,
                    "tantivy_doc_count": tantivy_doc_count,
                    "unresolved_links": unresolved_links,
                    "expired_docs": expired_docs,
                    "expired_claims": coord_stats.get("expired_claims", 0),
                    "tantivy_last_updated": tantivy_last_updated,
                    "chroma_last_updated": chroma_last_updated,
                    # Graph stats
                    "graph_node_count": graph_stats.get("nodes", 0),
                    # graph_edge_count reflects ALL edges in the NetworkX graph,
                    # including edges that point to __unresolved__ placeholder
                    # nodes (i.e. wiki-links whose target document does not yet
                    # exist).  It therefore equals resolved_edges + unresolved_links,
                    # not just resolved edges.  Compare with unresolved_links above
                    # to infer the resolved-only edge count if needed.
                    "graph_edge_count": graph_stats.get("edges", 0),
                }


def _format_sse(event: LithosEvent) -> str:
    """Format a LithosEvent as an SSE message string.

    Output format::

        id: <event-uuid>
        event: note.created
        data: {"agent": "az", "title": "Acme Pricing", ...}

    """
    # Envelope fields (agent, tags, timestamp) always win — strip reserved keys
    # from the payload copy so they cannot shadow the envelope values.
    user_data = {**event.payload}
    user_data.pop("agent", None)
    user_data.pop("tags", None)
    user_data.pop("timestamp", None)
    payload = {
        "agent": event.agent,
        **user_data,
        "tags": event.tags,
        "timestamp": event.timestamp.isoformat(),
    }
    data = json.dumps(payload, default=str)
    return f"id: {event.id}\nevent: {event.type}\ndata: {data}\n\n"


class _FileChangeHandler(FileSystemEventHandler):
    """Handle file system events for index updates."""

    def __init__(self, server: LithosServer, loop: asyncio.AbstractEventLoop):
        self.server = server
        self._loop = loop

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule_update(Path(str(event.src_path)))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule_update(Path(str(event.src_path)))

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule_update(Path(str(event.src_path)), deleted=True)

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
    def _log_future_exception(future: "concurrent.futures.Future[None]") -> None:
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
