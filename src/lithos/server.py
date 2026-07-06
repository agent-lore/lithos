"""Lithos MCP Server - FastMCP server exposing all tools."""

import asyncio
import collections
import contextlib
import dataclasses
import hashlib
import json
import logging
import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from lithos.cognitive_memory import CognitiveMemory
from lithos.config import LithosConfig, get_config, set_config
from lithos.coordination import CoordinationService, Task, TaskStatus
from lithos.edge_store import EdgeStore
from lithos.envelopes import (
    coordination_error_envelope,
    error_envelope,
    invalid_input_envelope,
)
from lithos.errors import CoordinationError, SearchBackendError
from lithos.events import (
    TASK_CANCELLED,
    TASK_CLAIMED,
    TASK_COMPLETED,
    TASK_CREATED,
    TASK_RELEASED,
    TASK_REOPENED,
    TASK_UPDATED,
    EventBus,
    LithosEvent,
)
from lithos.graph import KnowledgeGraph
from lithos.intake import CorpusIntake, DeleteRequest, NoteUpdateRequest, WriteRequest
from lithos.knowledge import (
    _UNSET,
    VALID_ACCESS_SCOPES,
    VALID_NOTE_TYPES,
    VALID_STATUSES,
    KnowledgeManager,
    _normalize_datetime,
    _UnsetType,
    validate_extra_metadata,
    validate_metadata_match,
)
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
    tool_metrics,
)
from lithos.tools import register_all
from lithos.watch_intake import WatchIntake

logger = logging.getLogger(__name__)


# Practical upper bound for ``lithos_list(content_query=...)``. Tantivy
# returns up to this many hits before Python-side filters run. A million
# matches from a single FTS query would already be degenerate; at that
# point the caller should tighten the query, not ask for more results.
_CONTENT_QUERY_FTS_CAP = 1_000_000


def _serialize_task_record(task: Task | TaskStatus) -> dict[str, Any]:
    """Render a Task or TaskStatus as the MCP wire-shape task dict.

    Used by both ``lithos_task_get`` (which serialises a ``Task``) and
    ``lithos_task_status`` (which serialises the task-shaped subset of a
    ``TaskStatus``, then layers ``claims`` on top). Centralising the field
    set + datetime formatting here stops the two responses drifting on
    additions or ISO serialisation choices.

    Claims are deliberately excluded — ``lithos_task_status`` adds them
    alongside this dict; ``lithos_task_get`` does not return them.
    """
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "task_type": task.task_type,
        "created_by": task.created_by,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "resolved_at": task.resolved_at.isoformat() if task.resolved_at else None,
        "tags": task.tags,
        "metadata": task.metadata,
        "outcome": task.outcome,
    }


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

        # Register all tools: extracted domain modules first (lithos.tools),
        # then the handlers not yet migrated out of _register_tools.
        register_all(self.mcp, self)
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
                # Ensure directories exist
                self.config.ensure_directories()

                # Build the search engine eagerly so the embedding model is
                # loaded before any request arrives. Tests may pre-inject a
                # ``search`` instance (e.g. a MagicMock) to avoid the real
                # backend cost.
                if self.search is None:
                    self.search = await SearchEngine.create(self._config)

                # Construct the EdgeStore directly and inject the same
                # instance into the projection and the intake (ADR-0006
                # Slice 1, issue #263). The projection owns
                # projection-class rows; ``CorpusIntake.assert_edge`` owns
                # asserted-class rows. They share the SQLite handle so
                # there is exactly one writer per server. Tests may
                # pre-inject either ``self.edge_store`` or
                # ``self.projection`` to skip real SQLite.
                if self.edge_store is None:
                    self.edge_store = EdgeStore(self._config)
                    await self.edge_store.open()
                if self.projection is None:
                    self.projection = await ProvenanceProjection.create(
                        self._config, edge_store=self.edge_store
                    )

                # Build the Corpus intake before the CognitiveMemory Module.
                # ADR-0006 Slice 1 (#263) makes ``CognitiveMemory`` declare
                # ``CorpusIntake`` as a constructor dependency so that
                # ``edge_upsert`` routes through ``intake.assert_edge``.
                # Tests that pre-inject ``self.intake`` keep that injection.
                if self.intake is None:
                    self.intake = CorpusIntake(
                        knowledge=self.knowledge,
                        search=self.search,
                        graph=self.graph,
                        coordination=self.coordination,
                        event_bus=self.event_bus,
                        edge_store=self.edge_store,
                    )

                # Build the watcher intake — peer of CorpusIntake (ADR-0007).
                # Same late-binding idiom: tests that pre-inject
                # ``self.watch_intake`` keep their injection.
                if self.watch_intake is None:
                    self.watch_intake = WatchIntake(
                        knowledge=self.knowledge,
                        search=self.search,
                        graph=self.graph,
                        event_bus=self.event_bus,
                        watch_path=self.config.storage.knowledge_path,
                    )

                # Build the CognitiveMemory Module eagerly (ADR-0005, issue
                # #255). The transitional ``attach_coordination`` setter
                # supplies the CoordinationService that ``EnrichWorker``
                # requires until coordination consolidates into the Module
                # per ADR-0005's "Anticipated evolution" section.
                if self.memory is None:
                    self.memory = await CognitiveMemory.create(
                        config=self._config,
                        knowledge=self.knowledge,
                        search=self.search,
                        graph=self.graph,
                        projection=self.projection,
                        event_bus=self.event_bus,
                        intake=self.intake,
                    )
                    self.memory.attach_coordination(self.coordination)

                # Run LCMA schema migrations through the Module so the lcma
                # boundary stays locked (issue #262).
                await self.memory.run_schema_migrations()

                # Initialize coordination database
                await self.coordination.initialize()

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
            schema_version: int | None = None,
            namespace: str | None = None,
            access_scope: str | None = None,
            note_type: str | None = None,
            status: str | None = None,
            summaries: dict | None = None,
            metadata: dict | None = None,
        ) -> dict[str, Any]:
            """Create or update a knowledge file.

            Args are grouped below by role. The grouping is documentation only —
            all parameters remain flat at the MCP boundary. See
            `docs/plans/unified-write-contract.md` for the normative field
            contract.

            Args:
                title: Title of the knowledge item.
                content: Markdown content (without frontmatter).
                agent: Your agent identifier.

                --- Identity & metadata ---
                id: UUID to update existing; omit to create new.
                tags: List of tags. On update: null/omit preserves existing; [] clears
                    all tags; non-empty list replaces.
                metadata: Free-form key/value dict persisted into the document's
                    frontmatter. On update: null/omit preserves existing; {} clears
                    all metadata; a non-empty dict is an additive per-key merge (a
                    key whose value is null deletes it). Keys must be strings and
                    must not collide with reserved frontmatter fields (e.g. title,
                    tags, version) — such writes are rejected with code "invalid_input".
                confidence: Confidence score 0-1 (default: 1.0 on create). On update:
                    null/omit preserves existing; float sets new value. Integers
                    are coerced to float; anything else that is not a finite
                    number in [0.0, 1.0] (non-numeric, bool, NaN/inf, or
                    out-of-range) is rejected with code "invalid_input".
                path: Where to store the note. Two accepted forms:
                    - Subdirectory (e.g., "procedures") — the filename is derived
                      from the title (slugified) and ".md" is appended.
                    - Full relative file path ending in ".md"
                      (e.g., "procedures/my-doc.md") — the final segment is used
                      as the filename verbatim; the title does NOT influence the
                      filename in this mode.
                    Intermediate path segments may not end in ".md"; such inputs
                    are rejected with code "invalid_input" to prevent
                    accidental creation of directories whose names end in ".md".

                --- Provenance ---
                source_url: URL provenance for this knowledge. On update: null/omit
                    preserves existing; "" clears; string sets new value.
                derived_from_ids: List of source document UUIDs this note was derived
                    from. On update: null/omit preserves existing; [] clears;
                    non-empty list replaces.
                source_task: Task ID this knowledge came from.

                --- Freshness ---
                ttl_hours: Time-to-live in hours from now. Computes expires_at.
                    Mutually exclusive with expires_at.
                expires_at: Absolute ISO 8601 expiry datetime. On update: null/omit
                    preserves existing; "" clears; ISO string sets new value.
                    Mutually exclusive with ttl_hours.

                --- Concurrency ---
                expected_version: If provided on update, reject with version_conflict if the
                    document's current version differs. Omit to skip version checking.
                    On create, this parameter is silently ignored.

                --- LCMA ---
                schema_version: LCMA schema version (default 1 on create).
                namespace: LCMA namespace. Persisted only if explicitly passed;
                    derived at read time otherwise.
                access_scope: shared|task|agent_private (default shared on create).
                    task requires source_task.
                note_type: observation|agent_finding|summary|concept|task_record|hypothesis
                    (default observation on create).
                status: active|archived|quarantined (default active on create).
                summaries: Optional dict with short/long summary strings.

            Returns:
                On success: {"status": "created"|"updated", "id", "path", "version",
                "warnings"}. Actionable outcomes keep their own top-level status:
                duplicate / slug_collision / path_collision / version_conflict
                (version_conflict carries "current_version"). All other failures
                use the standard error envelope {"status": "error", "code",
                "message"} — validation failures carry code "invalid_input".
            """
            logger.info("lithos_write agent=%s title=%r update=%s", agent, title, id is not None)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.write") as span:
                span.set_attribute("lithos.tool", "lithos_write")
                span.set_attribute("lithos.agent", agent)
                span.set_attribute("lithos.is_update", id is not None)

                # Validate ttl_hours / expires_at mutual exclusion
                if ttl_hours is not None and expires_at is not None:
                    return invalid_input_envelope(
                        "Provide either ttl_hours or expires_at, not both."
                    )

                # Validate ttl_hours
                if ttl_hours is not None and (
                    not isinstance(ttl_hours, (int, float))
                    or math.isnan(ttl_hours)
                    or math.isinf(ttl_hours)
                    or ttl_hours <= 0
                ):
                    return invalid_input_envelope("ttl_hours must be a finite positive number.")

                # Validate LCMA enum fields
                if access_scope is not None and access_scope not in VALID_ACCESS_SCOPES:
                    return invalid_input_envelope(
                        f"Invalid access_scope: {access_scope!r}. "
                        f"Must be one of {sorted(VALID_ACCESS_SCOPES)}"
                    )
                if note_type is not None and note_type not in VALID_NOTE_TYPES:
                    return invalid_input_envelope(
                        f"Invalid note_type: {note_type!r}. "
                        f"Must be one of {sorted(VALID_NOTE_TYPES)}"
                    )
                if status is not None and status not in VALID_STATUSES:
                    return invalid_input_envelope(
                        f"Invalid status: {status!r}. Must be one of {sorted(VALID_STATUSES)}"
                    )

                # Validate summaries shape
                if summaries is not None:
                    if not isinstance(summaries, dict):
                        return invalid_input_envelope(
                            "summaries must be an object with 'short' and/or 'long' string fields."
                        )
                    unknown_keys = set(summaries.keys()) - {"short", "long"}
                    if unknown_keys:
                        return invalid_input_envelope(
                            f"summaries has unknown keys: {sorted(unknown_keys)}. "
                            f"Allowed keys: ['long', 'short']."
                        )
                    for k, v in summaries.items():
                        if not isinstance(v, str):
                            return invalid_input_envelope(
                                f"summaries.{k} must be a string, got {type(v).__name__}."
                            )

                # Validate metadata shape at the boundary for a fast, clean
                # envelope. The same rule is enforced in the storage layer
                # (KnowledgeManager) so the invariant holds for every caller.
                if metadata is not None:
                    try:
                        validate_extra_metadata(metadata)
                    except ValueError as e:
                        return invalid_input_envelope(str(e))

                # Validate task-scope create-time invariant. The update-time
                # case is enforced under ``_write_lock`` inside
                # ``KnowledgeManager.update`` to avoid the TOCTOU window
                # described in ADR-0003.
                if access_scope == "task" and id is None and not source_task:
                    return invalid_input_envelope("access_scope='task' requires source_task")

                # Emit freshness span attributes
                if ttl_hours is not None:
                    span.set_attribute("freshness.ttl_hours", ttl_hours)
                elif expires_at is not None and expires_at != "":
                    span.set_attribute("freshness.expires_at_set", True)

                # Compute expires_at_dt from ttl_hours or expires_at string
                expires_at_dt: datetime | None | _UnsetType
                if ttl_hours is not None:
                    expires_at_dt = datetime.now(UTC) + timedelta(hours=ttl_hours)
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
                                expires_at_dt = expires_at_dt.replace(tzinfo=UTC)
                            else:
                                expires_at_dt = expires_at_dt.astimezone(UTC)
                        except ValueError:
                            return invalid_input_envelope(
                                f"Invalid expires_at datetime: {expires_at}"
                            )
                else:
                    # Create path: None means no expiry, str → parse
                    if expires_at is None:
                        expires_at_dt = None
                    else:
                        try:
                            expires_at_dt = datetime.fromisoformat(expires_at)
                            if expires_at_dt.tzinfo is None:
                                expires_at_dt = expires_at_dt.replace(tzinfo=UTC)
                            else:
                                expires_at_dt = expires_at_dt.astimezone(UTC)
                        except ValueError:
                            return invalid_input_envelope(
                                f"Invalid expires_at datetime: {expires_at}"
                            )

                # Translate MCP wire shape into intake field semantics:
                #   None  (omitted) → _UNSET (preserve)
                #   ""               → None (clear)
                #   value            → set
                # ``expires_at_dt`` already encodes this for the freshness
                # field above; we mirror the same rule for the rest here.
                if id is not None:
                    url_arg: str | None | _UnsetType
                    if source_url is None:
                        url_arg = _UNSET
                    elif source_url == "":
                        url_arg = None
                    else:
                        url_arg = source_url
                    prov_arg: list[str] | None | _UnsetType = (
                        _UNSET if derived_from_ids is None else derived_from_ids
                    )
                    tags_arg: list[str] | _UnsetType = _UNSET if tags is None else tags
                    conf_arg: float | _UnsetType = _UNSET if confidence is None else confidence
                    source_arg: str | None | _UnsetType = (
                        _UNSET if source_task is None else source_task
                    )
                    sv_arg: int | _UnsetType = _UNSET if schema_version is None else schema_version
                    ns_arg: str | None | _UnsetType = _UNSET if namespace is None else namespace
                    as_arg: str | None | _UnsetType = (
                        _UNSET if access_scope is None else access_scope
                    )
                    nt_arg: str | None | _UnsetType = _UNSET if note_type is None else note_type
                    st_arg: str | None | _UnsetType = _UNSET if status is None else status
                    sum_arg: dict | None | _UnsetType = _UNSET if summaries is None else summaries
                    meta_arg: dict | _UnsetType = _UNSET if metadata is None else metadata
                else:
                    # Create path forwards raw values; KnowledgeManager.create
                    # applies its own defaults.
                    url_arg = source_url or None
                    prov_arg = derived_from_ids
                    tags_arg = tags  # type: ignore[assignment]
                    conf_arg = confidence  # type: ignore[assignment]
                    source_arg = source_task
                    sv_arg = schema_version  # type: ignore[assignment]
                    ns_arg = namespace
                    as_arg = access_scope
                    nt_arg = note_type
                    st_arg = status
                    sum_arg = summaries
                    meta_arg = metadata if metadata is not None else _UNSET

                request = WriteRequest(
                    title=title,
                    content=content,
                    id=id,
                    tags=tags_arg,
                    confidence=conf_arg,
                    path=path,
                    source_task=source_arg,
                    source_url=url_arg,
                    derived_from_ids=prov_arg,
                    expires_at=expires_at_dt,
                    expected_version=expected_version,
                    schema_version=sv_arg,
                    namespace=ns_arg,
                    access_scope=as_arg,
                    note_type=nt_arg,
                    lcma_status=st_arg,
                    summaries=sum_arg,
                    metadata=meta_arg,
                )

                outcome = await self.intake.write(agent, request)

                if outcome.status == "slug_collision":
                    span.set_attribute("lithos.write_status", "slug_collision")
                    return {
                        "status": "slug_collision",
                        "message": outcome.message,
                        "existing_id": outcome.slug_collision_existing_id,
                        "warnings": [],
                    }
                if outcome.status == "path_collision":
                    span.set_attribute("lithos.write_status", "path_collision")
                    return {
                        "status": "path_collision",
                        "message": outcome.message,
                        "existing_id": outcome.path_collision_existing_id,
                        "warnings": list(outcome.warnings),
                    }
                if outcome.status == "duplicate":
                    span.set_attribute("lithos.write_status", "duplicate")
                    dup = outcome.duplicate_of
                    return {
                        "status": "duplicate",
                        "duplicate_of": {
                            "id": dup.id,
                            "title": dup.title,
                            "source_url": dup.source_url,
                        }
                        if dup
                        else None,
                        "message": outcome.message,
                        "warnings": list(outcome.warnings),
                    }
                if outcome.status == "version_conflict":
                    # An actionable write outcome, not an error: read-merge-write
                    # retry loops branch on this status (see CHANGELOG).
                    span.set_attribute("lithos.write_status", "version_conflict")
                    conflict: dict[str, Any] = {
                        "status": "version_conflict",
                        "message": outcome.message,
                        "warnings": list(outcome.warnings),
                    }
                    if outcome.current_version is not None:
                        conflict["current_version"] = outcome.current_version
                    return conflict
                if outcome.status in ("invalid_input", "content_too_large", "error"):
                    span.set_attribute("lithos.write_status", outcome.status)
                    if outcome.status == "invalid_input":
                        return invalid_input_envelope(outcome.message or "")
                    code = "internal_error" if outcome.status == "error" else outcome.status
                    return error_envelope(code, outcome.message or "")

                doc = outcome.document
                assert doc is not None
                span.set_attribute("lithos.doc_id", doc.id)
                span.set_attribute("lithos.write_status", outcome.status)
                span.set_attribute(
                    "lithos.provenance.source_count",
                    len(doc.metadata.derived_from_ids),
                )
                if outcome.warnings:
                    span.set_attribute("lithos.provenance.warning_count", len(outcome.warnings))
                logger.info("lithos_write completed doc_id=%s status=%s", doc.id, outcome.status)
                return {
                    "status": outcome.status,
                    "id": doc.id,
                    "path": str(doc.path),
                    "version": doc.metadata.version,
                    "warnings": list(outcome.warnings),
                }

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_note_update(
            id: str,
            agent: str,
            title: str | None = None,
            tags: list[str] | None = None,
            status: str | None = None,
            metadata: dict | None = None,
            expected_version: int | None = None,
        ) -> dict[str, Any]:
            """Patch a note's frontmatter (tags/metadata/title/status) without resending its body.

            The note counterpart to ``lithos_task_update``: a per-field patch
            that leaves the markdown body untouched. Use this instead of
            ``lithos_write`` whenever you only need to change frontmatter — it
            removes the read → reconstruct-body → write round-trip (and the
            lost-update risk that comes with reproducing the body), since the
            body is never read into the request at all.

            At least one of ``title``, ``tags``, ``status``, or ``metadata`` must
            be provided.

            Args:
                id: UUID of the note to patch.
                agent: Your agent identifier.
                title: New title. null/omit preserves existing. Renaming may
                    change the note's slug; a collision with another note's
                    slug is rejected as ``slug_collision``.
                tags: null/omit preserves existing; ``[]`` clears all tags; a
                    non-empty list replaces them.
                status: active|archived|quarantined. null/omit preserves existing.
                metadata: Additive per-key merge into the note's existing
                    frontmatter metadata. A key whose value is null deletes it;
                    other keys are set; keys not mentioned are preserved. As with
                    ``lithos_task_update`` there is no wholesale-clear affordance —
                    ``metadata={}`` makes no metadata change (and, with no other
                    field set, is rejected with code "invalid_input"). Keys must be
                    strings and must not collide with reserved frontmatter fields
                    (e.g. title, tags, version) — such patches are rejected with
                    code "invalid_input".
                expected_version: If provided, reject with version_conflict when
                    the note's current version differs. Omit to skip the check.

            Returns:
                On success: {"status": "updated", ...}. Actionable outcomes keep
                their own top-level status: slug_collision / duplicate /
                version_conflict (the latter carries "current_version"). All other
                failures use the standard error envelope {"status": "error",
                "code", "message"} — validation failures carry code
                "invalid_input"; an unknown id carries code "note_not_found".
            """
            # An empty metadata dict carries no change (it maps to _UNSET below),
            # so it does not count as a provided field — `metadata={}` alone is
            # rejected rather than producing a version-bumping, event-emitting no-op.
            has_metadata_change = metadata is not None and metadata != {}
            if title is None and tags is None and status is None and not has_metadata_change:
                return invalid_input_envelope(
                    "At least one of title, tags, status, or metadata must be provided "
                    "(an empty metadata dict makes no change)."
                )

            logger.info("lithos_note_update agent=%s id=%s", agent, id)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.note_update") as span:
                span.set_attribute("lithos.tool", "lithos_note_update")
                span.set_attribute("lithos.agent", agent)
                span.set_attribute("lithos.doc_id", id)

                # Validate status enum at the boundary for a fast, clean envelope.
                if status is not None and status not in VALID_STATUSES:
                    return invalid_input_envelope(
                        f"Invalid status: {status!r}. Must be one of {sorted(VALID_STATUSES)}"
                    )

                # Validate metadata shape at the boundary. The same rule is
                # enforced in KnowledgeManager so the invariant holds for every
                # caller; checking here gives a fast, clean envelope.
                if metadata is not None:
                    try:
                        validate_extra_metadata(metadata)
                    except ValueError as e:
                        return invalid_input_envelope(str(e))

                # Translate the MCP wire shape into KnowledgeManager semantics:
                #   None (omitted) → _UNSET (preserve)
                #   tags=[]        → clear all tags
                #   metadata={}    → _UNSET (no-op, mirroring lithos_task_update)
                tags_arg: list[str] | _UnsetType = _UNSET if tags is None else tags
                st_arg: str | None | _UnsetType = _UNSET if status is None else status
                meta_arg: dict | _UnsetType = (
                    _UNSET if metadata is None or metadata == {} else metadata
                )

                request = NoteUpdateRequest(
                    id=id,
                    title=title,
                    tags=tags_arg,
                    lcma_status=st_arg,
                    metadata=meta_arg,
                    expected_version=expected_version,
                )

                try:
                    outcome = await self.intake.note_update(agent, request)
                except FileNotFoundError:
                    span.set_attribute("lithos.write_status", "note_not_found")
                    return error_envelope("note_not_found", f"Note {id} not found")

                if outcome.status == "slug_collision":
                    span.set_attribute("lithos.write_status", "slug_collision")
                    return {
                        "status": "slug_collision",
                        "message": outcome.message,
                        "existing_id": outcome.slug_collision_existing_id,
                        "warnings": [],
                    }
                if outcome.status == "duplicate":
                    span.set_attribute("lithos.write_status", "duplicate")
                    dup = outcome.duplicate_of
                    return {
                        "status": "duplicate",
                        "duplicate_of": {
                            "id": dup.id,
                            "title": dup.title,
                            "source_url": dup.source_url,
                        }
                        if dup
                        else None,
                        "message": outcome.message,
                        "warnings": list(outcome.warnings),
                    }
                if outcome.status == "version_conflict":
                    # An actionable write outcome, not an error: read-merge-write
                    # retry loops branch on this status (see CHANGELOG).
                    span.set_attribute("lithos.write_status", "version_conflict")
                    conflict: dict[str, Any] = {
                        "status": "version_conflict",
                        "message": outcome.message,
                        "warnings": list(outcome.warnings),
                    }
                    if outcome.current_version is not None:
                        conflict["current_version"] = outcome.current_version
                    return conflict
                if outcome.status in ("invalid_input", "error"):
                    span.set_attribute("lithos.write_status", outcome.status)
                    if outcome.status == "invalid_input":
                        return invalid_input_envelope(outcome.message or "")
                    return error_envelope("internal_error", outcome.message or "")

                doc = outcome.document
                assert doc is not None
                span.set_attribute("lithos.write_status", outcome.status)
                logger.info(
                    "lithos_note_update completed doc_id=%s status=%s", doc.id, outcome.status
                )
                return {
                    "status": outcome.status,
                    "id": doc.id,
                    "path": str(doc.path),
                    "version": doc.metadata.version,
                    "warnings": list(outcome.warnings),
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
                    return error_envelope("doc_not_found", str(e))

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
                # Free-form key/value metadata (#305) as an isolated dict, so
                # callers can read back exactly what they wrote via the
                # lithos_write `metadata` param without sifting reserved fields.
                meta["extra"] = dict(doc.metadata.extra)
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

                outcome = await self.intake.delete(agent, DeleteRequest(id=id))
                if outcome.status == "not_found":
                    return error_envelope("doc_not_found", f"Document not found: {id}")
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
            entities: list[str] | None = None,
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
                entities: Filter results to documents whose ``entities``
                          frontmatter contains every named entity (exact
                          match, AND). Applies to all modes; resolved via an
                          inverted index and applied as a post-filter.
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
                    return error_envelope(
                        "invalid_mode",
                        f"Unknown search mode {mode!r}. Valid values: hybrid, fulltext, semantic, graph.",
                    )

                # Entities are not a per-backend filter: resolve the candidate
                # set once from the knowledge inverted index (#316) and
                # post-filter every mode's hits. Over-fetch to compensate,
                # mirroring the engine's own post-filter heuristic.
                entity_candidates = self.knowledge.entities_candidate_ids(entities)
                if entity_candidates is not None and not entity_candidates:
                    # No document carries every requested entity — skip the
                    # backend search entirely.
                    return {"results": []}
                fetch_limit = limit * 5 if entity_candidates is not None else limit

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
                # hybrid_search) and the mutating methods (index, remove) are
                # all wrapped in asyncio.to_thread() so Tantivy commits and ChromaDB embedding
                # don't block the event loop. Concurrent read+write is not protected by a lock,
                # but tantivy-py and ChromaDB are thread-safe for these operations. The
                # embedding model is loaded eagerly in SearchEngine.create() at server
                # startup, so no model-init race is reachable here.
                if mode == "fulltext":
                    ft_results = await asyncio.to_thread(
                        self.search.full_text_search,
                        query=query,
                        limit=fetch_limit,
                        tags=tags,
                        author=author,
                        path_prefix=path_prefix,
                    )
                    results_payload = [_build_result(r) for r in ft_results]
                elif mode == "semantic":
                    sem_results = await asyncio.to_thread(
                        self.search.semantic_search,
                        query=query,
                        limit=fetch_limit,
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
                        limit=fetch_limit,
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
                        limit=fetch_limit,
                        threshold=threshold,
                        tags=tags,
                        author=author,
                        path_prefix=path_prefix,
                    )
                    results_payload = [_build_result(r) for r in hybrid_results]

                if entity_candidates is not None:
                    results_payload = [r for r in results_payload if r["id"] in entity_candidates][
                        :limit
                    ]

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

        # ==================== LCMA Retrieval ====================

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_retrieve(
            query: str,
            limit: int = 10,
            namespace_filter: list[str] | None = None,
            agent_id: str | None = None,
            task_id: str | None = None,
            surface_conflicts: bool = False,
            max_context_nodes: int | None = None,
            tags: list[str] | None = None,
            path_prefix: str | None = None,
        ) -> dict[str, Any]:
            """LCMA cognitive retrieval — runs seven scouts with reranking.

            Orchestrates parallel scouts against the knowledge base, applies
            merge-and-normalize, Terrace 1 reranking, and writes an audit
            receipt on every call.

            Args:
                query: Search query string (required)
                limit: Max results (default: 10)
                namespace_filter: Restrict to these namespaces
                agent_id: Caller identity for access-scope gating and audit
                task_id: Task context — activates task_context scout and
                    working-memory tracking
                surface_conflicts: Reserved for MVP 2 contradiction surfacing
                max_context_nodes: Provenance seed count (defaults to limit)
                tags: Filter by tags (AND semantics)
                path_prefix: Filter by path prefix

            Returns:
                Dict with results list (superset of lithos_search result
                schema), temperature, terrace_reached, and receipt_id.
            """
            logger.info(
                "lithos_retrieve: called",
                extra={
                    "query_len": len(query),
                    "limit": limit,
                    "agent_id": agent_id,
                    "task_id": task_id,
                    "namespace_filter": namespace_filter,
                    "surface_conflicts": surface_conflicts,
                },
            )
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.retrieve") as span:
                span.set_attribute("lithos.tool", "lithos_retrieve")
                span.set_attribute("lithos.query.length", len(query))
                span.set_attribute("lithos.limit", limit)

                # Envelope-shaping short-circuit: when LCMA is disabled the
                # CognitiveMemory.retrieve precondition would fail, so we
                # surface a typed error response instead. The Module method
                # itself stays free of envelope concerns.
                if not self._config.lcma.enabled:
                    logger.warning("lithos_retrieve: LCMA is disabled")
                    return error_envelope("lcma_disabled", "LCMA is disabled via configuration")

                result = await self.memory.retrieve(
                    query=query,
                    limit=limit,
                    namespace_filter=namespace_filter,
                    agent_id=agent_id,
                    task_id=task_id,
                    surface_conflicts=surface_conflicts,
                    max_context_nodes=max_context_nodes,
                    tags=tags,
                    path_prefix=path_prefix,
                )

                result_count = len(result.get("results", []))  # type: ignore[union-attr]
                span.set_attribute("lithos.result_count", result_count)
                logger.info(
                    "lithos_retrieve: completed",
                    extra={
                        "result_count": result_count,
                        "receipt_id": result.get("receipt_id"),  # type: ignore[union-attr]
                        "temperature": result.get("temperature"),  # type: ignore[union-attr]
                        "terrace_reached": result.get("terrace_reached"),  # type: ignore[union-attr]
                        "agent_id": agent_id,
                        "task_id": task_id,
                    },
                )
                return result  # type: ignore[return-value]

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
            metadata_match: dict | None = None,
            entities: list[str] | None = None,
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
                entities: Filter by entity names from the document's ``entities``
                    frontmatter (AND across the list, exact match). Resolved via
                    an inverted index (no full scan).
                metadata_match: Filter by free-form metadata (AND across keys). For
                    each ``key: q`` a document matches when its stored metadata
                    value equals ``q`` or is a list containing ``q`` (so a note with
                    ``github_repos: ["org/a","org/b"]`` matches
                    ``{"github_repos": "org/a"}``). Query values must be scalars
                    (string/number/boolean); type-sensitive. Resolved via an
                    inverted index (no full scan).
                content_query: Filter by full-text search query (Tantivy).
                    Tantivy-native filters (``tags``, ``author``,
                    ``path_prefix``) are pushed down into the search query so
                    ranking runs over the already-filtered candidate set,
                    which is necessary for correctness under ranking pressure
                    (see #194). ``since`` and ``title_contains`` are applied
                    against the metadata cache after ranking.

            Returns:
                Dict with items list and total count
            """
            logger.info("lithos_list limit=%d offset=%d", limit, offset)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.list") as span:
                span.set_attribute("lithos.tool", "lithos_list")
                span.set_attribute("lithos.limit", limit)

                # Normalize to UTC so comparisons against normalized document
                # timestamps never mix naive and aware values.
                since_dt = None
                if since:
                    try:
                        since_dt = _normalize_datetime(datetime.fromisoformat(since))
                    except ValueError:
                        return invalid_input_envelope(f"Invalid since datetime: {since}")

                if metadata_match is not None:
                    try:
                        validate_metadata_match(metadata_match)
                    except ValueError as e:
                        return invalid_input_envelope(str(e))

                if content_query is not None:
                    # Push the Tantivy-native filters into the search call so
                    # ranking runs over the filtered candidate set. Using a
                    # global ranked window and then intersecting (the prior
                    # approach) silently dropped matches when filtered docs
                    # ranked deep globally — see #194.
                    try:
                        fts_results = await asyncio.to_thread(
                            self.search.full_text_search,
                            query=content_query,
                            limit=_CONTENT_QUERY_FTS_CAP,
                            tags=tags,
                            author=author,
                            path_prefix=path_prefix,
                        )
                    except SearchBackendError as exc:
                        return error_envelope(
                            "search_backend_error", f"Full-text search failed: {exc}"
                        )

                    # Apply the filters Tantivy doesn't handle: ``since``,
                    # ``title_contains`` and ``metadata_match``. Consult the
                    # metadata cache / inverted index so we don't incur a disk
                    # read per candidate. ``meta_candidates`` is None when no
                    # metadata filter is requested.
                    #
                    # Like ``since``/``title_contains``, ``metadata_match`` is a
                    # post-rank filter here: metadata isn't a Tantivy field, so
                    # it can't be pushed into the query the way tags/author/
                    # path_prefix are (#194). It runs against the full ranked
                    # window (cap = _CONTENT_QUERY_FTS_CAP = 1e6), so a match is
                    # only ever dropped if >1e6 docs match content_query — the
                    # same bound the other two post-filters already accept.
                    meta_candidates = self.knowledge.metadata_candidate_ids(metadata_match)
                    entity_candidates = self.knowledge.entities_candidate_ids(entities)
                    if (meta_candidates is not None and not meta_candidates) or (
                        entity_candidates is not None and not entity_candidates
                    ):
                        # An equality filter matched nothing — no point walking
                        # the ranked window.
                        fts_results = []
                    matching_ids: list[str] = []
                    for r in fts_results:
                        if meta_candidates is not None and r.id not in meta_candidates:
                            continue
                        if entity_candidates is not None and r.id not in entity_candidates:
                            continue
                        cached = self.knowledge.get_cached_meta(r.id)
                        if cached is None:
                            continue
                        if since_dt is not None:
                            cached_updated = _normalize_datetime(cached.updated_at)
                            if cached_updated < since_dt:
                                continue
                        if (
                            title_contains is not None
                            and title_contains.lower() not in (cached.title or "").lower()
                        ):
                            continue
                        matching_ids.append(r.id)

                    total = len(matching_ids)
                    page_ids = matching_ids[offset : offset + limit]
                    docs = []
                    for doc_id in page_ids:
                        try:
                            doc, _ = await self.knowledge.read(id=doc_id)
                            docs.append(doc)
                        except Exception:
                            # Cache can briefly lag the filesystem during
                            # reconcile; skipping mirrors list_all's behaviour.
                            continue
                elif title_contains is not None:
                    # ``title_contains`` has no Tantivy-backed fast path; fall
                    # back to list_all and filter in memory. Tracked in #201
                    # for a metadata-cache-only variant.
                    _, total_base = await self.knowledge.list_all(
                        path_prefix=path_prefix,
                        tags=tags,
                        author=author,
                        since=since_dt,
                        metadata_match=metadata_match,
                        entities=entities,
                        limit=0,
                        offset=0,
                    )
                    if total_base > 0:
                        all_docs, _ = await self.knowledge.list_all(
                            path_prefix=path_prefix,
                            tags=tags,
                            author=author,
                            since=since_dt,
                            metadata_match=metadata_match,
                            entities=entities,
                            limit=total_base,
                            offset=0,
                        )
                    else:
                        all_docs = []
                    all_docs = [d for d in all_docs if title_contains.lower() in d.title.lower()]
                    total = len(all_docs)
                    docs = all_docs[offset : offset + limit]
                else:
                    docs, total = await self.knowledge.list_all(
                        path_prefix=path_prefix,
                        tags=tags,
                        author=author,
                        since=since_dt,
                        metadata_match=metadata_match,
                        entities=entities,
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
                            "metadata": dict(d.metadata.extra),
                        }
                        for d in docs
                    ],
                    "total": total,
                }

        # ==================== Graph Tools ====================

        _RELATED_INCLUDES = ("links", "provenance", "edges")

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_related(
            id: str,
            include: list[str] | None = None,
            depth: int = 1,
            namespace: str | None = None,
        ) -> dict[str, Any]:
            """Composite "what is this document related to?" view.

            Merges three graph-query backends into a single response so agents
            don't have to fan out across multiple tools and mentally join the
            results:

            - **links** — structural ``[[wiki-link]]`` navigation (NetworkX).
            - **provenance** — ``derived_from_ids`` chains (frontmatter index).
            - **edges** — typed LCMA edges (edges.db), both directions.

            For edge-table queries that are not centred on a single document
            (e.g. "list all ``contradicts`` edges", "audit a namespace"), use
            :func:`lithos_edge_list` instead — that tool is the only way to
            express filters like ``type`` alone or ``to_id`` alone.

            Args:
                id: Document UUID.
                include: Subset of ``["links", "provenance", "edges"]`` to
                    populate. Defaults to all three. Unknown values are
                    silently ignored so forward-compatible callers don't
                    break when new backends land.
                depth: BFS depth 1-3 for ``links`` and ``provenance``.
                    Ignored for ``edges`` (LCMA edges are a flat table).
                namespace: Optional namespace filter applied to ``edges``.
                    ``links`` and ``provenance`` don't use namespaces.

            Returns:
                Dict shaped like::

                    {
                      "id": "<doc-id>",
                      "included": ["links", "provenance", "edges"],
                      "links": {"outgoing": [...], "incoming": [...]},
                      "provenance": {
                          "sources": [...], "derived": [...],
                          "unresolved_sources": [...]
                      },
                      "edges": {"outgoing": [...], "incoming": [...]},
                      "related_ids": ["<id>", ...]   # deduped union
                    }

                Sections not listed in ``include`` are omitted entirely
                (not emitted as empty keys). ``related_ids`` holds the
                deduped union of every id referenced across the included
                sections — sorted for determinism.
            """
            logger.info(
                "lithos_related: called",
                extra={"id": id, "include": include, "depth": depth, "namespace": namespace},
            )
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.related") as span:
                span.set_attribute("lithos.tool", "lithos_related")
                span.set_attribute("lithos.id", id)

                if not self.knowledge.has_document(id):
                    return error_envelope("doc_not_found", f"Document not found: {id}")

                requested = include if include is not None else list(_RELATED_INCLUDES)
                selected = [k for k in _RELATED_INCLUDES if k in requested]
                span.set_attribute("lithos.include", ",".join(selected))

                depth = min(max(depth, 1), 3)
                span.set_attribute("lithos.depth", depth)

                result: dict[str, Any] = {
                    "id": id,
                    "included": selected,
                }
                related_ids: set[str] = set()

                # --- links (NetworkX wiki-links) ---------------------------
                if "links" in selected:
                    links = self.graph.get_links(doc_id=id, direction="both", depth=depth)
                    outgoing = [{"id": ln.id, "title": ln.title} for ln in links.outgoing]
                    incoming = [{"id": ln.id, "title": ln.title} for ln in links.incoming]
                    result["links"] = {"outgoing": outgoing, "incoming": incoming}
                    related_ids.update(ln.id for ln in links.outgoing)
                    related_ids.update(ln.id for ln in links.incoming)

                # --- provenance (frontmatter derived_from_ids) -------------
                if "provenance" in selected:
                    sources = self._bfs_provenance(id, "sources", depth)
                    derived = self._bfs_provenance(id, "derived", depth)
                    unresolved_sources = sorted(self.knowledge.get_unresolved_sources(id))
                    result["provenance"] = {
                        "sources": sources,
                        "derived": derived,
                        "unresolved_sources": unresolved_sources,
                    }
                    related_ids.update(s["id"] for s in sources)
                    related_ids.update(d["id"] for d in derived)

                # --- edges (LCMA edges.db) ---------------------------------
                if "edges" in selected:
                    if self._config.lcma.enabled:
                        # Fan out both directions; caller rarely wants just one.
                        outgoing_edges = await self.projection.list_edges(
                            from_id=id, namespace=namespace
                        )
                        incoming_edges = await self.projection.list_edges(
                            to_id=id, namespace=namespace
                        )
                    else:
                        outgoing_edges = []
                        incoming_edges = []
                    result["edges"] = {
                        "outgoing": outgoing_edges,
                        "incoming": incoming_edges,
                    }
                    for edge in outgoing_edges:
                        tid = edge.get("to_id")
                        if isinstance(tid, str):
                            related_ids.add(tid)
                    for edge in incoming_edges:
                        fid = edge.get("from_id")
                        if isinstance(fid, str):
                            related_ids.add(fid)

                # Exclude the document itself from related_ids so callers
                # can iterate without filtering.
                related_ids.discard(id)
                result["related_ids"] = sorted(related_ids)
                span.set_attribute("lithos.related_count", len(related_ids))

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

        # ==================== Coordination Tools ====================

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_task_create(
            title: str,
            agent: str,
            description: str | None = None,
            tags: list[str] | None = None,
            metadata: dict[str, Any] | None = None,
            task_type: str = "task",
            depends_on: list[str] | None = None,
            parent_task_id: str | None = None,
        ) -> dict[str, Any]:
            """Create a new coordination task.

            Args:
                title: Task title
                agent: Creating agent identifier
                description: Task description
                tags: Task tags
                metadata: Arbitrary JSON metadata dict (optional). Must NOT contain
                    ``depends_on``/``blocked_on`` — dependencies are first-class task
                    edges now; pass ``depends_on`` instead.
                task_type: First-class task type: ``task``, ``epic``, or ``gate``.
                    A ``gate`` is an external wait and requires
                    ``metadata.gate_type`` in human/timer/ci/pr/external_task; a
                    ``timer`` gate also requires a parseable ``metadata.ready_at``
                    (ISO datetime). Link a task to a gate with a ``waits_on_gate``
                    edge; resolve a gate by completing it (``timer`` gates resolve
                    on their own once ``ready_at`` passes).
                depends_on: Predecessor task IDs. Each creates a ``blocks`` edge so
                    this task is not ready until that predecessor is completed.
                    Predecessors must already exist.
                parent_task_id: Optional parent. Creates a ``parent_child`` edge
                    ``parent -> this task``. The parent must exist; it may be any
                    task type (an ``epic`` or a plain ``task``).

            Returns:
                Dict with task_id, or an error envelope on validation failure.
            """
            logger.info("lithos_task_create agent=%s title=%r", agent, title)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_create") as span:
                span.set_attribute("lithos.tool", "lithos_task_create")
                span.set_attribute("lithos.agent", agent)
                try:
                    task_id = await self.coordination.create_task(
                        title=title,
                        agent=agent,
                        description=description,
                        tags=tags,
                        metadata=metadata,
                        task_type=task_type,
                        depends_on=depends_on,
                        parent_task_id=parent_task_id,
                    )
                except CoordinationError as exc:
                    span.set_attribute("lithos.success", False)
                    return coordination_error_envelope(exc)
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
            metadata: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """Update mutable task fields (title, description, tags, metadata).

            At least one of title, description, tags, or metadata must be provided.
            Works on terminal (completed/cancelled) tasks too — useful for annotating
            an archived task (e.g. a metadata snapshot) without reviving it; use
            ``lithos_task_reopen`` to bring a task back to active work. ``task_not_found``
            now means the task genuinely does not exist.

            ``metadata`` is applied as an additive per-key merge: keys with non-null
            values overwrite the existing value, keys whose value is ``None`` are
            deleted from the existing metadata, and keys not mentioned are preserved.
            To clear a specific key, pass ``{"key": None}``. There is no
            wholesale-clear affordance — ``metadata={}`` is a no-op that preserves
            all existing keys.

            Args:
                task_id: Task ID to update
                agent: Agent making the update
                title: New task title (optional)
                description: New task description (optional)
                tags: New task tags (optional)
                metadata: Per-key merge patch into the existing metadata dict
                    (optional). See merge contract above.

            Returns:
                Dict with success and message
            """
            if title is None and description is None and tags is None and metadata is None:
                return error_envelope(
                    "invalid_input",
                    "At least one of title, description, tags, or metadata must be provided",
                )

            logger.info("lithos_task_update task=%s agent=%s", task_id, agent)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_update") as span:
                span.set_attribute("lithos.tool", "lithos_task_update")
                span.set_attribute("lithos.agent", agent)
                span.set_attribute("lithos.task_id", task_id)
                try:
                    updated = await self.coordination.update_task(
                        task_id=task_id,
                        agent=agent,
                        title=title,
                        description=description,
                        tags=tags,
                        metadata=metadata,
                    )
                except CoordinationError as exc:
                    span.set_attribute("lithos.success", False)
                    return coordination_error_envelope(exc)
                span.set_attribute("lithos.success", updated)

                if updated:
                    await self._emit(
                        LithosEvent(
                            type=TASK_UPDATED,
                            agent=agent,
                            payload={"task_id": task_id},
                        )
                    )
                    return {"success": True, "message": f"Task {task_id} updated"}
                return error_envelope("task_not_found", f"Task {task_id} not found")

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
                    return error_envelope(
                        "claim_failed",
                        f"Could not claim aspect '{aspect}' on task '{task_id}': "
                        "task not found, not open, or aspect already claimed by another agent.",
                    )

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
                    return error_envelope(
                        "claim_not_found",
                        f"No active claim found for task '{task_id}', "
                        f"aspect '{aspect}', agent '{agent}'.",
                    )

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
                    return error_envelope(
                        "claim_not_found",
                        f"No matching claim found for task '{task_id}', "
                        f"aspect '{aspect}', agent '{agent}'.",
                    )

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
            outcome: str | None = None,
            cited_nodes: list[str] | None = None,
            misleading_nodes: list[str] | None = None,
            receipt_id: str | None = None,
        ) -> dict[str, Any]:
            """Mark a task as completed.

            Args:
                task_id: Task ID
                agent: Agent completing the task
                outcome: Optional free-text completion summary. Persisted on
                    the task row and forwarded in the ``task.completed`` event
                    payload so LCMA consolidation can use it as the frame
                    ``outcome`` slot.
                cited_nodes: Node IDs the agent found useful (None = no feedback)
                misleading_nodes: Node IDs the agent found misleading (None = no feedback)
                receipt_id: Specific receipt to bind feedback to (optional)

            Returns:
                Dict with success boolean, or error envelope if task not found or not open
            """
            outcome_len = len(outcome) if outcome else 0
            logger.info(
                "lithos_task_complete: called",
                extra={
                    "task_id": task_id,
                    "agent": agent,
                    "outcome_provided": outcome is not None,
                    "outcome_len": outcome_len,
                    "cited_count": len(cited_nodes) if cited_nodes is not None else 0,
                    "misleading_count": len(misleading_nodes)
                    if misleading_nodes is not None
                    else 0,
                    "receipt_id": receipt_id,
                },
            )
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_complete") as span:
                span.set_attribute("lithos.tool", "lithos_task_complete")
                span.set_attribute("lithos.agent", agent)
                span.set_attribute("lithos.task_id", task_id)
                span.set_attribute("lithos.outcome_provided", outcome is not None)
                span.set_attribute("lithos.outcome_len", outcome_len)
                # -- Validate feedback BEFORE completing the task --
                feedback_supplied = cited_nodes is not None or misleading_nodes is not None
                validated: dict[str, Any] | None = None
                if feedback_supplied:
                    error, validated = await self._validate_task_feedback(
                        task_id=task_id,
                        agent=agent,
                        cited_nodes=cited_nodes,
                        misleading_nodes=misleading_nodes,
                        receipt_id=receipt_id,
                    )
                    if error is not None:
                        return error

                success = await self.coordination.complete_task(
                    task_id=task_id,
                    agent=agent,
                    outcome=outcome,
                )
                span.set_attribute("lithos.success", success)

                if not success:
                    return error_envelope(
                        "task_not_found", f"Task '{task_id}' not found or not in an open state."
                    )

                # -- Apply reinforcement side-effects after task is completed --
                if validated is not None:
                    logger.info(
                        "lithos_task_complete: applying feedback reinforcement",
                        extra={
                            "task_id": task_id,
                            "agent": agent,
                            "cited_count": len(validated.get("cited") or []),
                            "misleading_count": len(validated.get("misleading") or []),
                            "ignored_count": len(validated.get("ignored") or []),
                        },
                    )
                    await self._apply_task_feedback(validated)

                await self._emit(
                    LithosEvent(
                        type=TASK_COMPLETED,
                        agent=agent,
                        payload={
                            "task_id": task_id,
                            "agent": agent,
                            "outcome": outcome,
                            "cited_nodes": json.dumps(cited_nodes),
                            "misleading_nodes": json.dumps(misleading_nodes),
                            "receipt_id": json.dumps(receipt_id),
                        },
                    )
                )

                # Surface tasks this completion just made ready (their last
                # blocking predecessor is now satisfied) so an orchestrator can
                # pick them up without re-polling lithos_task_ready.
                unblocked = await self.coordination.newly_unblocked_by(task_id)
                span.set_attribute("lithos.unblocked_count", len(unblocked))
                return {"success": True, "unblocked": unblocked}

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

                return error_envelope(
                    "task_not_found", f"Task {task_id} not found or already closed"
                )

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_task_reopen(
            task_id: str,
            agent: str,
        ) -> dict[str, Any]:
            """Reopen a terminal (completed/cancelled) task back to ``open``.

            The inverse of complete/cancel — use it to revive a task to active work
            (e.g. an accidental completion) and to remediate dependents stranded as
            ``blocker_unsatisfiable`` by a cancelled blocker/gate: reopening that
            blocker/gate returns its dependents to a waiting state. Clears
            ``resolved_at``/``outcome``, records the reopen as a ``[Reopened]``
            finding, and emits a ``task.reopened`` event.

            Args:
                task_id: Task ID to reopen
                agent: Agent performing the reopen

            Returns:
                ``{"success": true, "reblocked": [...]}`` — ``reblocked`` lists open
                dependents this reopen put back under the task's block (non-empty
                only when reopening a *completed* blocker/gate; a cancelled-task
                reopen un-strands dependents and re-blocks no one). On failure,
                ``{"status": "error", "code": "task_not_found" | "task_not_resolved",
                "message": ...}``.
            """
            logger.info("lithos_task_reopen task=%s agent=%s", task_id, agent)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_reopen") as span:
                span.set_attribute("lithos.tool", "lithos_task_reopen")
                span.set_attribute("lithos.agent", agent)
                span.set_attribute("lithos.task_id", task_id)
                try:
                    prior_status, prior_outcome = await self.coordination.reopen_task(
                        task_id=task_id, agent=agent
                    )
                except CoordinationError as exc:
                    span.set_attribute("lithos.success", False)
                    return coordination_error_envelope(exc)

                # Durable audit: a queryable finding recording the prior terminal state.
                summary = f"[Reopened] task reopened (was {prior_status})"
                if prior_outcome:
                    summary += f"; prior outcome: {prior_outcome}"
                await self.coordination.post_finding(task_id=task_id, agent=agent, summary=summary)

                await self._emit(
                    LithosEvent(
                        type=TASK_REOPENED,
                        agent=agent,
                        payload={
                            "task_id": task_id,
                            "agent": agent,
                            "prior_status": prior_status,
                            "prior_outcome": prior_outcome,
                        },
                    )
                )
                reblocked = await self.coordination.newly_reblocked_by(task_id, prior_status)
                span.set_attribute("lithos.reblocked_count", len(reblocked))
                return {"success": True, "reblocked": reblocked}

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_node_stats(
            node_id: str,
        ) -> dict[str, Any]:
            """View a note's salience score, retrieval stats, and penalty counts.

            Args:
                node_id: The document ID to look up stats for

            Returns:
                Dict with salience, retrieval_count, cited_count, ignored_count,
                misleading_count, and other stats fields.
                Returns error envelope if node_id does not match any known document.
            """
            logger.info("lithos_node_stats node_id=%s", node_id)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.node_stats") as span:
                span.set_attribute("lithos.tool", "lithos_node_stats")
                span.set_attribute("lithos.node_id", node_id)

                result = await self.memory.node_stats(node_id)
                return result if isinstance(result, dict) else dataclasses.asdict(result)

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_task_list(
            agent: str | None = None,
            status: str | None = None,
            tags: list[str] | None = None,
            since: str | None = None,
            resolved_since: str | None = None,
            with_claims: bool = False,
            metadata_match: dict | None = None,
            task_type: str | None = None,
        ) -> dict[str, list[dict[str, Any]]]:
            """List tasks with optional filters.

            Args:
                agent: Filter by creating agent
                status: Filter by status: "open", "completed", or "cancelled" (None = all)
                task_type: Filter by first-class task type (task/epic/gate)
                tags: Filter by tags (task must have all specified tags)
                metadata_match: Filter by metadata (AND across keys). For each
                    ``key: q`` a task matches when its stored metadata value
                    equals ``q`` or is a list containing ``q``. Query values must
                    be scalars (string/number/boolean); type-sensitive.
                since: Filter by created_at >= this ISO datetime string (e.g. "2024-01-01T00:00:00Z")
                resolved_since: Filter by resolved_at >= this ISO datetime string.
                    ``resolved_at`` is set on both terminal transitions (complete
                    and cancel), so this returns tasks resolved (in either way)
                    within the window. Open tasks and historical cancellations
                    from before the column was populated on cancel are excluded
                    automatically (their ``resolved_at`` is NULL).
                with_claims: When True, each task in the response includes its
                    active (non-expired) claims inline as a ``claims`` array
                    (same shape as ``lithos_task_status``). Defaults to False.
                    Use to avoid an N+1 of ``lithos_task_status`` calls when
                    rendering a list view that needs claim info.

            Returns:
                Dict with tasks list containing id, title, description, status,
                created_by, created_at, resolved_at, tags, metadata, outcome,
                and (when with_claims) claims.
            """
            logger.info(
                "lithos_task_list agent=%s status=%s tags=%s since=%s resolved_since=%s "
                "with_claims=%s",
                agent,
                status,
                tags,
                since,
                resolved_since,
                with_claims,
            )
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_list") as span:
                span.set_attribute("lithos.tool", "lithos_task_list")
                if agent:
                    span.set_attribute("lithos.agent", agent)
                if status:
                    span.set_attribute("lithos.status", status)
                span.set_attribute("lithos.with_claims", with_claims)

                if metadata_match is not None:
                    try:
                        validate_metadata_match(metadata_match)
                    except ValueError as e:
                        return invalid_input_envelope(str(e))

                tasks = await self.coordination.list_tasks(
                    agent=agent,
                    status=status,
                    tags=tags,
                    since=since,
                    resolved_since=resolved_since,
                    with_claims=with_claims,
                    metadata_match=metadata_match,
                    task_type=task_type,
                )
                return {"tasks": tasks}

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_task_edge_upsert(
            from_task_id: str,
            to_task_id: str,
            type: str,
            agent: str,
            metadata: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """Create or update a typed relation between two tasks.

            Edge types accepted in this phase: ``blocks`` (to_task is not ready
            until from_task is completed), ``parent_child`` (from_task is the
            parent; purely structural, never blocks), ``discovered_from`` (to_task
            was discovered while executing from_task; non-blocking), and
            ``waits_on_gate`` (to_task is not ready until the gate from_task is
            resolved — the gate is completed, or a ``timer`` gate whose
            ``ready_at`` has passed; a cancelled gate makes the waiter
            unsatisfiable).

            Args:
                from_task_id: Source task (blocker / parent / source).
                to_task_id: Target task (blocked / child / discovered).
                type: Edge type (see above).
                agent: Agent creating the edge.
                metadata: Optional edge metadata.

            Returns:
                ``{"success": true}`` or an error envelope (unaccepted type,
                self-edge, missing task, or a blocking edge that would create a
                dependency cycle).
            """
            logger.info(
                "lithos_task_edge_upsert from=%s to=%s type=%s agent=%s",
                from_task_id,
                to_task_id,
                type,
                agent,
            )
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_edge_upsert") as span:
                span.set_attribute("lithos.tool", "lithos_task_edge_upsert")
                span.set_attribute("lithos.agent", agent)
                span.set_attribute("lithos.edge_type", type)
                try:
                    await self.coordination.upsert_task_edge(
                        from_task_id=from_task_id,
                        to_task_id=to_task_id,
                        edge_type=type,
                        agent=agent,
                        metadata=metadata,
                    )
                except CoordinationError as exc:
                    span.set_attribute("lithos.success", False)
                    return coordination_error_envelope(exc)
                return {"success": True}

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_task_edge_list(
            task_id: str,
            direction: str = "both",
            types: list[str] | None = None,
        ) -> dict[str, Any]:
            """List edges touching a task.

            Args:
                task_id: Task whose edges to list.
                direction: "incoming", "outgoing", or "both" (default).
                types: Optional edge-type filter.

            Returns:
                ``{"edges": [...]}`` — each edge carries from/to/type, its
                ``direction`` relative to ``task_id``, metadata, and provenance.
            """
            logger.info("lithos_task_edge_list task=%s direction=%s", task_id, direction)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_edge_list") as span:
                span.set_attribute("lithos.tool", "lithos_task_edge_list")
                span.set_attribute("lithos.task_id", task_id)
                if direction not in ("incoming", "outgoing", "both"):
                    return error_envelope(
                        "invalid_input",
                        f"direction must be 'incoming', 'outgoing', or 'both', got {direction!r}.",
                    )
                edges = await self.coordination.list_task_edges(
                    task_id=task_id,
                    direction=direction,
                    types=types,
                )
                return {"edges": edges}

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_task_ready(
            project: str | None = None,
            tags: list[str] | None = None,
            metadata_match: dict | None = None,
            limit: int = 50,
            with_claims: bool = True,
        ) -> dict[str, Any]:
            """Return open tasks that are ready to work.

            A task is ready when it is open, not a gate/epic, has no incoming
            blocking edge whose predecessor is unsatisfied (every ``blocks``
            predecessor must be ``completed``), and is not blocked by a gate.
            Active claims are *attached* when ``with_claims`` but never used to
            exclude a task — claims are per-aspect and collision-safety comes
            from the atomic claim, so the picking agent decides what "taken" means.

            Args:
                project: Shorthand for ``metadata.project == project``.
                tags: Filter by tags (task must have all specified tags).
                metadata_match: Metadata filter (AND across keys); scalars only.
                limit: Maximum tasks to return.
                with_claims: Attach each task's active claims inline (default True).

            Returns:
                Dict with a ``tasks`` list (the feasible frontier).
            """
            logger.info(
                "lithos_task_ready project=%s tags=%s limit=%s with_claims=%s",
                project,
                tags,
                limit,
                with_claims,
            )
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_ready") as span:
                span.set_attribute("lithos.tool", "lithos_task_ready")
                if limit < 1:
                    return error_envelope("invalid_input", f"limit must be >= 1, got {limit}.")
                if metadata_match is not None:
                    try:
                        validate_metadata_match(metadata_match)
                    except ValueError as e:
                        return invalid_input_envelope(str(e))
                tasks = await self.coordination.list_ready(
                    project=project,
                    tags=tags,
                    metadata_match=metadata_match,
                    limit=limit,
                    with_claims=with_claims,
                )
                span.set_attribute("lithos.ready_count", len(tasks))
                return {"tasks": tasks}

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_task_blocked(
            project: str | None = None,
            tags: list[str] | None = None,
            metadata_match: dict | None = None,
            limit: int = 50,
        ) -> dict[str, Any]:
            """Return open tasks that are NOT ready, with structured blocker reasons.

            Same filter surface as ``lithos_task_ready``. Each returned task carries
            a ``blockers`` list; each blocker has ``kind``:
            ``task`` (predecessor still open — just waiting),
            ``gate`` (waiting on an unresolved gate),
            ``blocker_unsatisfiable`` (predecessor or gate was cancelled — needs
            intervention), or ``cycle`` (the dependency chain forms a cycle).

            Args:
                project: Shorthand for ``metadata.project == project``.
                tags: Filter by tags (task must have all specified tags).
                metadata_match: Metadata filter (AND across keys); scalars only.
                limit: Maximum tasks to return.

            Returns:
                Dict with a ``tasks`` list, each task including its ``blockers``.
            """
            logger.info("lithos_task_blocked project=%s tags=%s limit=%s", project, tags, limit)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_blocked") as span:
                span.set_attribute("lithos.tool", "lithos_task_blocked")
                if limit < 1:
                    return error_envelope("invalid_input", f"limit must be >= 1, got {limit}.")
                if metadata_match is not None:
                    try:
                        validate_metadata_match(metadata_match)
                    except ValueError as e:
                        return invalid_input_envelope(str(e))
                tasks = await self.coordination.list_blocked(
                    project=project,
                    tags=tags,
                    metadata_match=metadata_match,
                    limit=limit,
                )
                span.set_attribute("lithos.blocked_count", len(tasks))
                return {"tasks": tasks}

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_task_children(
            task_id: str,
            recursive: bool = False,
            include_closed: bool = False,
        ) -> dict[str, list[dict[str, Any]]]:
            """Return the child tasks of a parent/epic (via ``parent_child`` edges).

            Args:
                task_id: Parent (or epic) whose children to list.
                recursive: Walk the full descendant subtree, not just direct
                    children (default False).
                include_closed: Include completed/cancelled children in the
                    result (default False = open children only). The subtree is
                    traversed in full regardless, so an open grandchild under a
                    closed child is still surfaced.

            Returns:
                Dict with a ``tasks`` list (task records, same shape as
                ``lithos_task_list``).
            """
            logger.info(
                "lithos_task_children task=%s recursive=%s include_closed=%s",
                task_id,
                recursive,
                include_closed,
            )
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_children") as span:
                span.set_attribute("lithos.tool", "lithos_task_children")
                span.set_attribute("lithos.task_id", task_id)
                tasks = await self.coordination.list_children(
                    task_id=task_id,
                    recursive=recursive,
                    include_closed=include_closed,
                )
                span.set_attribute("lithos.children_count", len(tasks))
                return {"tasks": tasks}

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_task_spawn(
            source_task_id: str,
            title: str,
            agent: str,
            description: str | None = None,
            relation_type: str = "discovered_from",
            inherit_project: bool = True,
            inherit_tags: bool = True,
            inherit_context: bool = True,
            metadata: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """Create a follow-on task linked to an existing source task.

            The relation edge is always ``source -> spawned``:
            ``discovered_from`` records that the spawned task was found while
            executing the source (non-blocking); ``blocks`` makes the spawned task
            wait until the source is ``completed``.

            Args:
                source_task_id: The task this follow-on came from.
                title: Title for the spawned task.
                agent: Spawning agent identifier.
                description: Optional description for the spawned task.
                relation_type: ``discovered_from`` (default) or ``blocks``.
                inherit_project: Copy ``metadata.project`` from the source.
                inherit_tags: Copy the source's tags.
                inherit_context: Copy scheduling-convention metadata (priority,
                    parallelizable, phase) from the source.
                metadata: Extra metadata; overrides inherited keys. Must NOT
                    contain ``depends_on``/``blocked_on``.

            Returns:
                Dict with task_id, or an error envelope (unknown source, invalid
                relation_type, or forbidden metadata key).
            """
            logger.info(
                "lithos_task_spawn source=%s agent=%s relation=%s",
                source_task_id,
                agent,
                relation_type,
            )
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_spawn") as span:
                span.set_attribute("lithos.tool", "lithos_task_spawn")
                span.set_attribute("lithos.agent", agent)
                span.set_attribute("lithos.relation_type", relation_type)
                try:
                    task_id = await self.coordination.spawn_task(
                        source_task_id=source_task_id,
                        title=title,
                        agent=agent,
                        description=description,
                        relation_type=relation_type,
                        inherit_project=inherit_project,
                        inherit_tags=inherit_tags,
                        inherit_context=inherit_context,
                        metadata=metadata,
                    )
                except CoordinationError as exc:
                    span.set_attribute("lithos.success", False)
                    return coordination_error_envelope(exc)
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
        async def lithos_task_status(
            task_id: str,
        ) -> dict[str, list[dict[str, Any]]]:
            """Get the full record of a specific task with its active claims.

            Args:
                task_id: Task ID to look up

            Returns:
                Dict with tasks list containing id, title, description, status,
                created_by, created_at, resolved_at, tags, metadata, outcome,
                and claims. Returns an empty tasks list if the task does not
                exist (mirrors the historical behaviour).
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
                            **_serialize_task_record(s),
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
        async def lithos_task_get(
            task_id: str,
        ) -> dict[str, Any]:
            """Get the full record of a single task by ID.

            Returns the task on its own (not wrapped in a list) so callers
            that already know the ID don't have to unwrap a one-element
            response. Does not include claims — use ``lithos_task_status``
            when you need claims alongside the task fields.

            Args:
                task_id: Task ID to look up

            Returns:
                Dict with task fields (id, title, description, status,
                created_by, created_at, resolved_at, tags, metadata, outcome).
                Returns the standard error envelope
                ``{status: "error", code: "task_not_found", message: ...}``
                when no task matches.
            """
            logger.info("lithos_task_get task_id=%s", task_id)
            tracer = get_tracer()
            with tracer.start_as_current_span("lithos.tool.task_get") as span:
                span.set_attribute("lithos.tool", "lithos_task_get")
                span.set_attribute("lithos.task_id", task_id)

                task = await self.coordination.get_task(task_id)
                if task is None:
                    return error_envelope("task_not_found", f"Task '{task_id}' not found.")

                return {"task": _serialize_task_record(task)}


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
