"""Lithos MCP Server - FastMCP server exposing all tools."""

import asyncio
import contextlib
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from lithos.cognitive_memory import CognitiveMemory
from lithos.config import LithosConfig, get_config, set_config
from lithos.coordination import CoordinationService
from lithos.edge_store import EdgeStore
from lithos.envelopes import (
    error_envelope,
)
from lithos.events import (
    EventBus,
    LithosEvent,
)
from lithos.graph import KnowledgeGraph
from lithos.intake import CorpusIntake
from lithos.knowledge import (
    KnowledgeManager,
)
from lithos.pipeline import build_pipeline
from lithos.provenance import ProvenanceProjection
from lithos.search import Healthy, SearchEngine
from lithos.telemetry import (
    StatusCode,
    get_tracer,
    lithos_metrics,
    register_active_claims_observer,
    register_lcma_metrics,
    register_resource_gauges,
    register_sse_active_clients_observer,
)
from lithos.tools import register_all
from lithos.watch_intake import WatchIntake

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
        # ``search`` is created asynchronously in :meth:`initialize` via
        # ``SearchEngine.create`` so the embedding model is loaded eagerly
        # before any caller can observe an unloaded engine.
        self.knowledge = KnowledgeManager(self._config)
        self.search: SearchEngine = None  # type: ignore[assignment]
        self.graph = KnowledgeGraph(self._config)
        self.coordination = CoordinationService(self._config)
        self.event_bus = EventBus(self._config.events)
        # CorpusIntake is built in initialize() once self.search exists.
        self.intake: CorpusIntake = None  # type: ignore[assignment]
        # WatchIntake (ADR-0007) is built in initialize() alongside CorpusIntake,
        # after the late-bound SearchEngine is ready. Watcher events have no
        # agent and never register one, so the Module takes the four view-layer
        # collaborators only.
        self.watch_intake: WatchIntake = None  # type: ignore[assignment]

        # ``edge_store`` is constructed directly in :meth:`initialize` and
        # injected into both ``ProvenanceProjection`` and ``CorpusIntake``
        # so a single SQLite handle backs the projection-class and
        # asserted-class edge rows (ADR-0006 Slice 1, issue #263).
        self.projection: ProvenanceProjection = None  # type: ignore[assignment]
        self.edge_store: EdgeStore = None  # type: ignore[assignment]

        # ``CognitiveMemory`` (ADR-0005, issue #255) owns the ``StatsStore``
        # and the ``EnrichWorker`` lifecycles. The server no longer holds
        # back-aliases to either (issue #262 — the lcma boundary lock):
        # callers route through ``self.memory.<method>`` instead.
        self.memory: CognitiveMemory = None  # type: ignore[assignment]

        # Cached count fields for synchronous OTEL observable gauge callbacks.
        # Primed at startup by _refresh_coordination_stats_cache() and kept fresh
        # by _coordination_stats_refresh_loop() so the gauges don't report 0
        # until the first lithos_stats call (see #181).
        self._cached_active_claims: int = 0
        self._cached_agent_count: int = 0

        # How often the background task refreshes _cached_agent_count /
        # _cached_active_claims from the coordination DB. Small enough that
        # observability dashboards stay in sync with reality; large enough
        # that it's not a measurable load on the DB.
        self._coordination_stats_refresh_seconds: float = 30.0
        self._coordination_stats_refresh_task: asyncio.Task[None] | None = None

        # Background tasks (kept to prevent garbage collection)
        self._background_tasks: set[asyncio.Task[None]] = set()

        # Create FastMCP app
        self.mcp = FastMCP(
            "Lithos",
            instructions="Local shared knowledge base for AI agents",
        )

        # SSE delivery: capacity-gated by an asyncio semaphore (#206).
        # Replaces a plain int counter whose check-then-increment was a soft
        # race. Single-threaded asyncio makes ``locked()`` + ``acquire()``
        # atomic when no ``await`` lies between them — see
        # :meth:`_try_acquire_sse_slot`.
        self._sse_semaphore: asyncio.Semaphore = asyncio.BoundedSemaphore(
            self._config.events.max_sse_clients
        )

        # Register all tools (lithos.tools domain modules)
        register_all(self.mcp, self)

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

    def build_http_app(self) -> Starlette:
        """Build a single ASGI app exposing both MCP HTTP transports (#304).

        Lithos serves two transports on one port so any compliant MCP client
        can connect without a bridge:

        - ``POST /mcp`` — StreamableHTTP (MCP 2025-03-26+), stateless. All
          Lithos state lives in the knowledge base (SQLite, ChromaDB,
          Tantivy), never in the MCP session, so stateless mode is the right
          fit — each request is independent.
        - ``GET /sse`` + ``POST /messages/`` — legacy SSE, unchanged so
          existing clients keep working.

        The StreamableHTTP app is the base because its lifespan is a superset
        of the SSE app's: both run FastMCP's ``_lifespan_manager`` (idempotent
        — the second entry is a no-op), and the StreamableHTTP lifespan
        additionally runs the session manager. The SSE transport needs nothing
        beyond ``_lifespan_manager``, so its transport routes (``/sse`` and the
        ``/messages`` mount) are appended to the base app and served under the
        base lifespan. Custom routes (``/events``, ``/health``, ``/audit``) are
        registered via ``custom_route`` and therefore appear in *both*
        sub-apps; the SSE copies are filtered out by path to avoid duplicates.

        Splicing the SSE routes into the StreamableHTTP app is sound only while
        both apps carry the *same* app-level middleware — which they do today:
        with no auth configured, FastMCP gives each app just
        ``RequestContextMiddleware``, so the base app's stack covers the spliced
        routes identically. Configuring FastMCP auth would diverge the two
        stacks (per-transport auth routes + middleware), so this method refuses
        to run under auth rather than silently serving the SSE routes without
        their auth wiring — revisit the composition before enabling auth.
        """
        if self.mcp.auth is not None:
            raise NotImplementedError(
                "build_http_app composes the SSE and StreamableHTTP transports by "
                "splicing routes under a shared middleware stack, which assumes no "
                "per-transport auth wiring. FastMCP auth is configured — rework the "
                "composition (e.g. mount each transport app with its own middleware) "
                "before enabling it."
            )

        # ``transport="http"`` is FastMCP's alias for StreamableHTTP.
        streamable_app = self.mcp.http_app(path="/mcp", transport="http", stateless_http=True)
        sse_app = self.mcp.http_app(path="/sse", transport="sse")

        existing_paths = {getattr(route, "path", None) for route in streamable_app.router.routes}
        for route in sse_app.router.routes:
            if getattr(route, "path", None) not in existing_paths:
                streamable_app.router.routes.append(route)

        return streamable_app

    async def serve_http(
        self, host: str, port: int, uvicorn_config: dict[str, Any] | None = None
    ) -> None:
        """Serve both MCP HTTP transports via uvicorn until cancelled.

        Mirrors the uvicorn configuration FastMCP uses in ``run_http_async``
        (graceful-shutdown disabled, ASGI lifespan enabled, sans-io
        websockets). The app's own lifespan — enabled by ``lifespan="on"`` —
        runs the FastMCP lifespan manager and the StreamableHTTP session
        manager, so no extra wrapping is required.

        Args:
            host: Interface to bind.
            port: TCP port to bind.
            uvicorn_config: Extra uvicorn ``Config`` kwargs (e.g. ``log_config``)
                merged over the defaults.
        """
        import uvicorn

        app = self.build_http_app()
        config_kwargs: dict[str, Any] = {
            "timeout_graceful_shutdown": 0,
            "lifespan": "on",
            "ws": "websockets-sansio",
        }
        if uvicorn_config:
            config_kwargs.update(uvicorn_config)

        config = uvicorn.Config(app, host=host, port=port, **config_kwargs)
        await uvicorn.Server(config).serve()

    async def _emit(self, event: LithosEvent) -> None:
        """Emit an event, logging any failure without propagating."""
        try:
            await self.event_bus.emit(event)
        except Exception:
            logger.exception("Failed to emit %s event", event.type)

    async def _validate_task_feedback(
        self,
        *,
        task_id: str,
        agent: str,
        cited_nodes: list[str] | None,
        misleading_nodes: list[str] | None,
        receipt_id: str | None,
    ) -> tuple[dict[str, Any], None] | tuple[None, dict[str, Any]]:
        """Validate receipt and compute filtered node sets without side effects.

        Returns ``(error_envelope, None)`` on hard failure, or
        ``(None, validated_data)`` on success.  ``validated_data`` contains the
        keys ``cited``, ``misleading``, ``ignored`` (each a list[str] or None)
        and ``skip`` (bool — True when feedback should be silently dropped).
        """
        # -- Resolve receipt --
        receipt: dict[str, object] | None
        if receipt_id is not None:
            receipt = await self.memory.get_receipt(receipt_id, task_id)
            if receipt is None:
                return (
                    error_envelope(
                        "receipt_not_found",
                        f"Receipt '{receipt_id}' not found or does not belong to task '{task_id}'.",
                    ),
                    None,
                )
        else:
            receipt = await self.memory.get_latest_receipt(task_id, agent)
            if receipt is None:
                logger.warning(
                    "No receipt found for task=%s agent=%s — dropping all feedback",
                    task_id,
                    agent,
                )
                return (None, {"skip": True, "cited": None, "misleading": None, "ignored": []})

        receipt_node_ids: set[str] = set()
        raw_ids = receipt.get("final_node_ids")
        if isinstance(raw_ids, list):
            receipt_node_ids = {str(nid) for nid in raw_ids}

        # -- Intersect with receipt node IDs --
        cited = list(receipt_node_ids & set(cited_nodes)) if cited_nodes is not None else None
        misleading = (
            list(receipt_node_ids & set(misleading_nodes)) if misleading_nodes is not None else None
        )

        # Log dropped IDs
        if cited_nodes is not None:
            dropped = set(cited_nodes) - receipt_node_ids
            for nid in dropped:
                logger.debug("Dropped cited node %s — not in receipt", nid)
        if misleading_nodes is not None:
            dropped = set(misleading_nodes) - receipt_node_ids
            for nid in dropped:
                logger.debug("Dropped misleading node %s — not in receipt", nid)

        # -- Intersect with existing knowledge (prevent writes for deleted notes) --
        existing_ids: set[str] = set()
        for nid in receipt_node_ids:
            if self.knowledge.get_cached_meta(nid) is not None:
                existing_ids.add(nid)

        if cited is not None:
            cited = [nid for nid in cited if nid in existing_ids]
        if misleading is not None:
            misleading = [nid for nid in misleading if nid in existing_ids]

        # -- Compute ignored: receipt nodes not in cited or misleading --
        cited_set = set(cited) if cited is not None else set()
        misleading_set = set(misleading) if misleading is not None else set()
        ignored = [
            nid
            for nid in receipt_node_ids
            if nid not in cited_set and nid not in misleading_set and nid in existing_ids
        ]

        return (
            None,
            {"skip": False, "cited": cited, "misleading": misleading, "ignored": ignored},
        )

    async def _apply_task_feedback(self, validated: dict[str, Any]) -> None:
        """Apply reinforcement side-effects using pre-validated data."""
        if validated.get("skip"):
            return

        cited = validated["cited"]
        misleading = validated["misleading"]
        ignored = validated["ignored"]

        if cited:
            await self.memory.reinforce_cited(cited)
            await self.memory.reinforce_between(cited)

        if misleading:
            await self.memory.reinforce_misleading(misleading)

        if ignored:
            await self.memory.reinforce_ignored(ignored)

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

        # Check the search engine (composes Tantivy, Chroma, and embedding-model probes)
        try:
            search_status = await asyncio.to_thread(self.search.health)
            if isinstance(search_status, Healthy):
                components["search"] = {"status": "ok"}
            else:
                components["search"] = {"status": "unavailable", "error": search_status.reason}
        except Exception as e:
            components["search"] = {"status": "unavailable", "error": str(e)}

        # Check knowledge base
        try:
            await self.knowledge.list_all(limit=1)
            components["knowledge_base"] = {"status": "ok"}
        except Exception as e:
            components["knowledge_base"] = {"status": "unavailable", "error": str(e)}

        overall = "ok" if all(c["status"] == "ok" for c in components.values()) else "degraded"
        return {
            "status": overall,
            "timestamp": datetime.now(UTC).isoformat(),
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

        # Atomic capacity gate: locked() + acquire() are not preempted in
        # single-threaded asyncio because acquire() returns synchronously when
        # the semaphore is not locked (no ``await`` between observe and
        # decrement). Replaces a soft-race int-counter check (#206).
        if not await self._try_acquire_sse_slot():
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
                        except TimeoutError:
                            # Send keepalive comment to prevent proxy/firewall disconnects
                            yield ": keepalive\n\n"
                        except asyncio.CancelledError:
                            break
                except Exception as exc:
                    conn_span.record_exception(exc)
                    conn_span.set_status(StatusCode.ERROR, str(exc))
                    logger.exception("SSE stream error")
                finally:
                    self._sse_semaphore.release()
                    self.event_bus.unsubscribe(queue)

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    async def _try_acquire_sse_slot(self) -> bool:
        """Non-blocking acquire of an SSE-client slot (#206).

        Single-threaded asyncio guarantees atomicity: ``Semaphore.acquire``
        returns synchronously when the semaphore is not locked, so the
        ``locked()`` check and the decrement happen in the same event-loop
        tick with no opportunity for a concurrent coroutine to race.
        """
        if self._sse_semaphore.locked():
            return False
        await self._sse_semaphore.acquire()
        return True

    def _sse_active_count(self) -> int:
        """Number of currently-acquired SSE slots — backs the OTEL gauge.

        ``asyncio.Semaphore`` does not expose remaining capacity through a
        public API, so the active count is computed as ``max - available``
        via the documented internal ``_value`` attribute.
        """
        return self._config.events.max_sse_clients - self._sse_semaphore._value  # type: ignore[attr-defined]

    async def initialize(self) -> None:
        """Initialize all components."""
        _init_start = time.perf_counter()
        tracer = get_tracer()
        with tracer.start_as_current_span("lithos.server.initialize") as span:
            span.set_attribute("lithos.server.host", self._config.server.host)
            span.set_attribute("lithos.server.port", self._config.server.port)

            try:
                # Build the component graph through the shared factory so the
                # server and the CLI wire it identically — including the
                # one-EdgeStore-writer invariant of ADR-0006 Slice 1 (#263).
                # Passing the current attributes through preserves the
                # test-injection seam: anything a test pre-injected (e.g. a
                # MagicMock ``search``, to skip the real embedding backend) is
                # adopted as-is rather than rebuilt.
                pipeline = await build_pipeline(
                    self._config,
                    knowledge=self.knowledge,
                    search=self.search,
                    graph=self.graph,
                    coordination=self.coordination,
                    event_bus=self.event_bus,
                    edge_store=self.edge_store,
                    projection=self.projection,
                    intake=self.intake,
                    memory=self.memory,
                )
                self.search = pipeline.search
                self.edge_store = pipeline.edge_store
                self.projection = pipeline.projection
                self.intake = pipeline.intake
                self.memory = pipeline.memory

                # Build the watcher intake — peer of CorpusIntake (ADR-0007).
                # Server-only: nothing outside a long-running process watches
                # the filesystem, so it stays out of the shared factory. Same
                # late-binding idiom — tests that pre-inject keep their
                # injection.
                if self.watch_intake is None:
                    self.watch_intake = WatchIntake(
                        knowledge=self.knowledge,
                        search=self.search,
                        graph=self.graph,
                        event_bus=self.event_bus,
                        watch_path=self.config.storage.knowledge_path,
                    )

                # Run LCMA schema migrations through the Module so the lcma
                # boundary stays locked (issue #262). Lifecycle, not
                # construction — hence here rather than in the factory.
                await self.memory.run_schema_migrations()

                # Prime the coordination stats cache BEFORE registering OTEL
                # gauges — otherwise the first scrape would read the initial
                # zero values, masking real agent/claim counts on dashboards
                # (see #181).
                await self._refresh_coordination_stats_cache()

                # Start periodic background refresh so agent counts etc. stay
                # in sync without requiring an explicit lithos_stats call.
                self._start_coordination_stats_refresh()

                # Register active claims gauge observer
                register_active_claims_observer(lambda: self._cached_active_claims)

                # Register SSE active clients gauge observer
                register_sse_active_clients_observer(self._sse_active_count)

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

                # Load or build indices. ``SearchEngine.create`` already opened
                # Tantivy and ran the schema-version check; ``needs_initial_rebuild``
                # surfaces that flag without reaching into the backend. After
                # #226 lands this becomes part of ``KnowledgeManager.plan_reconcile``.
                tantivy_needs_rebuild = self.search.needs_initial_rebuild()
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

                # The edge store is already open — ``ProvenanceProjection.create``
                # opens it eagerly above. No explicit ``open()`` needed here.

                # Start the CognitiveMemory Module FIRST — opens the
                # StatsStore and (when ``config.lcma.enabled``) starts the
                # EnrichWorker. This is the explicit lifecycle invariant
                # from issue #255: ``start()`` is the call that opens the
                # store, so the LCMA stats-cache priming and gauge
                # registration below must run after it.
                await self.memory.start()

                # Register LCMA observable gauges when LCMA is enabled.
                # Callbacks bind to ``CognitiveMemory`` methods so the lcma
                # boundary stays locked (issue #262).
                if self._config.lcma.enabled:
                    # Prime the LCMA stats cache BEFORE OTEL gauge registration
                    # for the same reason we prime coordination stats (#181):
                    # EnrichWorker refreshes the cache after each drain cycle,
                    # but the first drain is 5 minutes out by default — until
                    # then the gauges would report zero on a populated DB.
                    try:
                        await self.memory.refresh_cached_counts()
                    except Exception:
                        logger.warning(
                            "LCMA stats cache priming failed; gauges will "
                            "start at zero until the first EnrichWorker drain.",
                            exc_info=True,
                        )
                    register_lcma_metrics(
                        get_enrich_queue_depth=self.memory.get_cached_enrich_queue_depth,
                        get_coactivation_pairs=self.memory.get_cached_coactivation_pairs,
                        get_working_memory_active_tasks=self.memory.get_cached_working_memory_active_tasks,
                    )

            except Exception as exc:
                span.record_exception(exc)
                span.set_status(StatusCode.ERROR, str(exc))
                raise
            finally:
                # Record startup duration whether initialisation succeeded or failed.
                elapsed_ms = (time.perf_counter() - _init_start) * 1000
                lithos_metrics.startup_duration.record(elapsed_ms)
                span.set_attribute("lithos.startup_duration", elapsed_ms)

    async def _refresh_coordination_stats_cache(self) -> None:
        """Refresh cached coordination counts that back the OTEL gauges.

        OTEL observable-gauge callbacks must be synchronous and cheap; they
        therefore read from ``self._cached_agent_count`` and
        ``self._cached_active_claims`` rather than hitting the coordination DB
        inside the metric collection loop. This coroutine is the single place
        that refreshes those fields (called once at startup and then
        periodically from :meth:`_coordination_stats_refresh_loop`, and also
        opportunistically from the ``lithos_stats`` tool).

        Regression for #181: without this priming step the gauge callbacks
        reported 0 until the first ``lithos_stats`` call — so dashboards
        showed "0 registered agents" on a cold server even when many agents
        had registered.
        """
        try:
            coord_stats = await self.coordination.get_stats()
        except Exception:
            logger.warning(
                "Coordination stats refresh failed — OTEL gauges will keep "
                "stale values until next successful refresh.",
                exc_info=True,
            )
            return

        prev_agents = self._cached_agent_count
        prev_claims = self._cached_active_claims
        self._cached_agent_count = coord_stats.get("agents", 0)
        self._cached_active_claims = coord_stats.get("open_claims", 0)
        logger.debug(
            "Coordination stats cache refreshed",
            extra={
                "agents": self._cached_agent_count,
                "open_claims": self._cached_active_claims,
                "agents_delta": self._cached_agent_count - prev_agents,
                "claims_delta": self._cached_active_claims - prev_claims,
            },
        )

    def _start_coordination_stats_refresh(self) -> None:
        """Spawn the periodic stats-refresh background task, idempotently."""
        if (
            self._coordination_stats_refresh_task is not None
            and not self._coordination_stats_refresh_task.done()
        ):
            return
        task = asyncio.create_task(self._coordination_stats_refresh_loop())
        self._coordination_stats_refresh_task = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        logger.info(
            "Started coordination stats refresh loop",
            extra={"interval_seconds": self._coordination_stats_refresh_seconds},
        )

    async def _coordination_stats_refresh_loop(self) -> None:
        """Background task: periodically refresh the coordination stats cache.

        Exits cleanly on cancellation. Swallows per-iteration exceptions so a
        transient DB hiccup doesn't kill the whole refresh loop — the next
        tick retries.
        """
        interval = self._coordination_stats_refresh_seconds
        try:
            while True:
                await asyncio.sleep(interval)
                await self._refresh_coordination_stats_cache()
        except asyncio.CancelledError:
            logger.info("Coordination stats refresh loop cancelled")
            raise

    async def stop_coordination_stats_refresh(self) -> None:
        """Cancel the periodic stats-refresh task, if any."""
        task = self._coordination_stats_refresh_task
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            # Cancellation is expected; any other exception has already been
            # logged from inside the loop.
            await task
        self._coordination_stats_refresh_task = None

    def _safe_tantivy_count(self) -> int:
        """Return full-text document count, 0 on any error (OTEL gauge probe)."""
        try:
            return self.search.count_documents()
        except Exception:
            return 0

    def _safe_chroma_count(self) -> int:
        """Return semantic chunk count, 0 on any error (OTEL gauge probe).

        ``SearchEngine.count_chunks`` already returns 0 when the Chroma store
        is quarantined.
        """
        try:
            return self.search.count_chunks()
        except Exception:
            return 0

    async def _rebuild_indices(self) -> None:
        """Rebuild all search indices from files."""
        tracer = get_tracer()
        with tracer.start_as_current_span("lithos.index.rebuild") as span:
            # Hard reset so plan_reconcile sees an empty world and re-emits
            # an `add` per doc — preserves today's full-rebuild semantics.
            self.search.clear_all()
            self.graph.clear()
            self.knowledge.rescan()

            plan = await self.knowledge.plan_reconcile(
                search=self.search,
                graph=self.graph,
                projection=self.projection,
            )
            result = await self.knowledge.apply_reconcile(
                plan,
                search=self.search,
                graph=self.graph,
                projection=self.projection,
            )

            file_count = result.search.scanned if result.search else 0
            error_count = len(result.search.failed) if result.search else 0
            if result.search:
                for failure in result.search.failed:
                    logger.error("Error indexing %s backend: %s", failure.backend, failure.detail)

            span.set_attribute("lithos.file_count", file_count)
            span.set_attribute("lithos.error_count", error_count)
            # KnowledgeGraph._apply_reconcile flushes the cache itself
            # (graph.py:929-948); explicit save_cache() is redundant.

    async def stop_enrich_worker(self) -> None:
        """Stop the CognitiveMemory Module and close the projection.

        ADR-0005 (issue #255): the Module owns the EnrichWorker and the
        StatsStore lifecycle and must be stopped first; the projection
        owns the EdgeStore (ADR-0004) and is closed after the Module
        has released its worker. StatsStore and EdgeStore hold
        persistent SQLite connections (#172) — both must be closed to
        release WAL handles cleanly.
        """
        if self.memory is not None:
            await self.memory.stop()
        if self.projection is not None:
            await self.projection.close()

    async def shutdown(self) -> None:
        """Stop every background worker and close every persistent handle.

        Idempotent. Aggregates :meth:`stop_coordination_stats_refresh`,
        :meth:`stop_enrich_worker`, the :class:`WatchIntake` observer, and
        the graph-cache flush so callers — especially test fixtures — do
        not need to track which subsystems own which handles. Forgetting
        any one of these used to leave aiosqlite worker threads alive past
        test event-loop teardown, which surfaced as
        ``RuntimeError: Event loop is closed`` warnings and (on CI)
        job-hanging orphan processes.
        """
        await self.stop_coordination_stats_refresh()
        await self.stop_enrich_worker()
        if self.watch_intake is not None:
            await self.watch_intake.stop()
        # Force-flush the graph cache so pending mutations land on disk
        # before the process exits (#203). Owned by shutdown rather than
        # WatchIntake.stop because it's a graph-cache concern, not a
        # watcher concern (ADR-0007).
        if self.graph._dirty_ops > 0:
            self.graph.save_cache()


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
