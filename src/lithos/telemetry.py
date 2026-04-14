# pyright: reportPossiblyUnboundVariable=false
"""OpenTelemetry instrumentation for Lithos.

Provides tracing and metrics when opentelemetry-sdk is installed and
telemetry is enabled in config. Otherwise, all public functions are
safe no-ops.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from lithos.config import LithosConfig

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# --- Import guard: OTEL packages are optional ---

try:
    from opentelemetry import metrics, trace
    from opentelemetry.metrics import Observation
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )
    from opentelemetry.trace import StatusCode  # type: ignore[assignment]

    _HAS_OTEL = True
except ImportError:
    _HAS_OTEL = False

    class StatusCode:  # type: ignore[no-redef]
        """No-op StatusCode stub used when opentelemetry-api is not installed."""

        ERROR = "ERROR"
        OK = "OK"
        UNSET = "UNSET"


# --- Module state ---

_initialized = False
_event_bus_metrics_registered = False
_tracer_provider: Any = None  # TracerProvider when active
_meter_provider: Any = None  # MeterProvider when active
_log_provider: Any = None  # LoggerProvider when active
_trace_context_filter: _TraceContextFilter | None = None  # installed on root logger


# --- No-op stubs for when OTEL is absent or disabled ---


class _NoOpSpan:
    """Minimal no-op span."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, status: Any) -> None:
        pass

    def record_exception(self, exc: Exception) -> None:
        pass

    def __enter__(self) -> _NoOpSpan:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class _NoOpTracer:
    """No-op tracer that returns no-op spans."""

    def start_as_current_span(self, name: str, **kwargs: Any) -> _NoOpSpan:
        return _NoOpSpan()


class _NoOpCounter:
    """No-op counter instrument."""

    def add(self, amount: int | float, attributes: dict[str, Any] | None = None) -> None:
        pass


class _NoOpHistogram:
    """No-op histogram instrument."""

    def record(self, amount: float, attributes: dict[str, Any] | None = None) -> None:
        pass


class _NoOpMeter:
    """No-op meter that returns no-op instruments."""

    def create_counter(self, name: str, **kwargs: Any) -> _NoOpCounter:
        return _NoOpCounter()

    def create_histogram(self, name: str, **kwargs: Any) -> _NoOpHistogram:
        return _NoOpHistogram()

    def create_up_down_counter(self, name: str, **kwargs: Any) -> _NoOpCounter:
        return _NoOpCounter()

    def create_observable_gauge(self, name: str, **kwargs: Any) -> None:
        return None


# --- Lifecycle ---


def setup_telemetry(config: LithosConfig, *, _test_span_exporter: Any = None) -> None:
    """Initialize OTEL SDK. Call once at startup.

    Args:
        config: LithosConfig instance (uses config.telemetry settings).
        _test_span_exporter: For testing only -- an InMemorySpanExporter to
            capture spans without an OTLP endpoint.

    Safe to call even if OTEL packages are not installed or telemetry
    is disabled -- in both cases this is a no-op.
    """
    global _initialized, _tracer_provider, _meter_provider, _log_provider

    if _initialized:
        return

    if not config.telemetry.enabled:
        logger.debug("Telemetry disabled in config")
        return

    if not _HAS_OTEL:
        logger.warning(
            "Telemetry enabled but opentelemetry-sdk is not installed. "
            "Install with: uv sync --extra otel"
        )
        return

    # Read version from package metadata (not hardcoded)
    service_version = _get_package_version()

    resource = Resource.create(
        {
            "service.name": config.telemetry.service_name,
            "service.version": service_version,
        }
    )

    endpoint = config.telemetry.endpoint
    # Prefer signal-specific OTEL endpoints if provided, otherwise derive from base.
    traces_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    metrics_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
    if not traces_endpoint and endpoint:
        traces_endpoint = _signal_endpoint(endpoint, "traces")
    if not metrics_endpoint and endpoint:
        metrics_endpoint = _signal_endpoint(endpoint, "metrics")

    # --- Traces ---
    _tracer_provider = TracerProvider(resource=resource)

    if _test_span_exporter is not None:
        _tracer_provider.add_span_processor(SimpleSpanProcessor(_test_span_exporter))
    elif traces_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
            OTLPSpanExporter,
        )

        span_exporter = OTLPSpanExporter(endpoint=traces_endpoint)
        _tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    elif config.telemetry.console_fallback:
        _tracer_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(_tracer_provider)

    # --- Metrics ---
    metric_reader = None

    if metrics_endpoint:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (  # type: ignore[import-not-found]
            OTLPMetricExporter,
        )

        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=metrics_endpoint),
            export_interval_millis=config.telemetry.export_interval_ms,
        )
    elif config.telemetry.console_fallback:
        from opentelemetry.sdk.metrics.export import ConsoleMetricExporter

        metric_reader = PeriodicExportingMetricReader(
            ConsoleMetricExporter(),
            export_interval_millis=config.telemetry.export_interval_ms,
        )

    if metric_reader:
        _meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(_meter_provider)

    # --- Logs ---
    # Step 2: full OTEL log export when an endpoint is configured.
    logs_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")
    if not logs_endpoint and endpoint:
        logs_endpoint = _signal_endpoint(endpoint, "logs")

    if logs_endpoint:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (  # type: ignore[import-not-found]
            OTLPLogExporter,
        )
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

        _log_provider = LoggerProvider(resource=resource)
        _log_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=logs_endpoint))
        )
        set_logger_provider(_log_provider)

        # Attach OTEL handler to the root logger so all Python logs are exported.
        # The LoggingHandler also enriches records with trace context, but the
        # _inject_trace_context_into_logs() filter installed below ensures
        # trace_id/span_id are available even without an export endpoint.
        otel_handler = LoggingHandler(level=logging.DEBUG, logger_provider=_log_provider)
        logging.getLogger().addHandler(otel_handler)

    # Mark initialized before installing the filter so the filter sees a
    # consistent state from the moment it becomes active.
    _initialized = True

    # Step 1 (always active when tracing is on): inject trace_id + span_id into
    # every Python log record via a lightweight logging.Filter.  This enables
    # trace-log correlation in any log aggregator (Loki, Datadog, Elastic, etc.)
    # with zero additional dependencies.
    _inject_trace_context_into_logs(config.telemetry.service_name)

    logger.info("OpenTelemetry initialized (endpoint=%s)", endpoint)


def shutdown_telemetry() -> None:
    """Flush and shut down OTEL providers. Call on exit.

    Ensures in-flight spans are exported before the process terminates.
    Safe to call if telemetry was never initialized.
    """
    global _initialized, _tracer_provider, _meter_provider, _log_provider, _trace_context_filter

    if not _initialized:
        return

    if _tracer_provider is not None:
        _tracer_provider.force_flush()
        _tracer_provider.shutdown()
        _tracer_provider = None

    if _meter_provider is not None:
        _meter_provider.shutdown()
        _meter_provider = None

    if _log_provider is not None:
        _log_provider.force_flush()
        _log_provider.shutdown()
        _log_provider = None

    # Remove trace-context filter from root logger
    if _trace_context_filter is not None:
        logging.getLogger().removeFilter(_trace_context_filter)
        _trace_context_filter = None

    _initialized = False
    logger.debug("OpenTelemetry shut down")


class _TraceContextFilter(logging.Filter):
    """Logging filter that injects OTEL trace/span IDs into every log record.

    After installation, every ``LogRecord`` carries:

    * ``otelTraceID`` (str, 32 hex chars) — current trace ID, or ``"0" * 32``
    * ``otelSpanID``  (str, 16 hex chars) — current span ID, or ``"0" * 16``
    * ``otelTraceSampled`` (bool) — whether the current span is sampled
    * ``otelServiceName`` (str) — the configured service name

    These fields can be referenced in a log format string:

    .. code-block:: python

        "%(message)s traceId=%(otelTraceID)s spanId=%(otelSpanID)s"

    They are also forwarded to structured logging backends (Loki, Datadog,
    Elastic, etc.) that read ``LogRecord`` extras.

    This filter is installed once by :func:`_inject_trace_context_into_logs`
    and removed by :func:`shutdown_telemetry`.
    """

    def __init__(self, service_name: str = "lithos") -> None:
        super().__init__()
        self._service_name = service_name

    def filter(self, record: logging.LogRecord) -> bool:
        """Inject trace context into *record*.  Always returns True (pass-through)."""
        if _initialized:
            span = trace.get_current_span()
            span_context = span.get_span_context()
            if span_context is not None and span_context.is_valid:
                record.otelTraceID = format(span_context.trace_id, "032x")
                record.otelSpanID = format(span_context.span_id, "016x")
                record.otelTraceSampled = span_context.trace_flags.sampled
            else:
                record.otelTraceID = "0" * 32
                record.otelSpanID = "0" * 16
                record.otelTraceSampled = False
        else:
            record.otelTraceID = "0" * 32
            record.otelSpanID = "0" * 16
            record.otelTraceSampled = False
        record.otelServiceName = self._service_name
        return True


def _inject_trace_context_into_logs(service_name: str = "lithos") -> None:
    """Install :class:`_TraceContextFilter` on the root logger.

    Idempotent: a second call is a no-op if already installed.
    Injected fields: ``otelTraceID``, ``otelSpanID``, ``otelTraceSampled``,
    ``otelServiceName``.
    """
    global _trace_context_filter

    if _trace_context_filter is not None:
        return  # already installed

    root_logger = logging.getLogger()
    _trace_context_filter = _TraceContextFilter(service_name=service_name)
    root_logger.addFilter(_trace_context_filter)
    logger.debug("OTEL trace-context log filter installed")


def _get_package_version() -> str:
    """Read version from package metadata."""
    try:
        from importlib.metadata import version

        return version("lithos-mcp")
    except Exception:
        return "unknown"


def _signal_endpoint(base: str, signal: str) -> str:
    """Build OTLP HTTP signal endpoint from base collector URL."""
    base = base.rstrip("/")
    if base.endswith(f"/v1/{signal}"):
        return base
    if base.endswith("/v1/traces") and signal == "metrics":
        return base.replace("/v1/traces", "/v1/metrics")
    if base.endswith("/v1/metrics") and signal == "traces":
        return base.replace("/v1/metrics", "/v1/traces")
    return f"{base}/v1/{signal}"


# --- Accessors ---


def get_tracer(name: str = "lithos") -> Any:
    """Get a tracer. Returns a no-op tracer if OTEL is not active."""
    if _HAS_OTEL and _initialized:
        return trace.get_tracer(name)
    return _NoOpTracer()


def get_meter(name: str = "lithos") -> Any:
    """Get a meter. Returns a no-op meter if OTEL is not active."""
    if _HAS_OTEL and _initialized:
        return metrics.get_meter(name)
    return _NoOpMeter()


# --- @traced decorator ---


def traced(
    span_name: str | None = None,
    attributes: dict[str, str] | None = None,
) -> Callable[[F], F]:
    """Decorator that wraps a function in an OTEL span.

    Works for both sync and async functions. When OTEL is disabled,
    the decorator is a transparent pass-through (minimal overhead).

    Args:
        span_name: Span name. Defaults to "lithos.{module}.{function}".
        attributes: Static attributes to add to every span.

    Usage:
        @traced("lithos.search.fulltext")
        def full_text_search(self, query, ...):
            ...

        @traced()  # auto-generates span name
        async def create_task(self, ...):
            ...
    """

    def decorator(func: F) -> F:
        name = span_name or f"lithos.{func.__module__.rsplit('.', 1)[-1]}.{func.__name__}"

        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                tracer = get_tracer()
                with tracer.start_as_current_span(name) as span:
                    if attributes:
                        for k, v in attributes.items():
                            span.set_attribute(k, v)
                    try:
                        return await func(*args, **kwargs)
                    except Exception as exc:
                        span.record_exception(exc)
                        if _HAS_OTEL and _initialized:
                            span.set_status(trace.StatusCode.ERROR)
                        raise

            return async_wrapper  # type: ignore[return-value]
        else:

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                tracer = get_tracer()
                with tracer.start_as_current_span(name) as span:
                    if attributes:
                        for k, v in attributes.items():
                            span.set_attribute(k, v)
                    try:
                        return func(*args, **kwargs)
                    except Exception as exc:
                        span.record_exception(exc)
                        if _HAS_OTEL and _initialized:
                            span.set_status(trace.StatusCode.ERROR)
                        raise

            return sync_wrapper  # type: ignore[return-value]

    return decorator


# --- @timed_write decorator ---


def timed_write(op: str) -> Callable[[F], F]:
    """Decorator that records knowledge write duration to ``lithos.knowledge.write_duration_ms``.

    Designed to stack with ``@traced``:

    .. code-block:: python

        @traced("lithos.knowledge.create")
        @timed_write("create")
        async def create(self, ...) -> WriteResult:
            ...

    Records on every exit (normal return *and* exception).  The ``success``
    attribute is ``True`` when no exception propagates (i.e. even if the
    returned ``WriteResult`` has ``status="error"`` — that is a controlled
    outcome, not a crash).

    Args:
        op: Operation name — ``"create"`` or ``"update"``.
    """

    def decorator(func: F) -> F:
        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                _t0 = time.perf_counter()
                _success = True
                try:
                    return await func(*args, **kwargs)
                except Exception:
                    _success = False
                    raise
                finally:
                    elapsed_ms = (time.perf_counter() - _t0) * 1000
                    lithos_metrics.knowledge_write_duration.record(
                        elapsed_ms, {"op": op, "success": _success}
                    )

            return async_wrapper  # type: ignore[return-value]
        else:

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                _t0 = time.perf_counter()
                _success = True
                try:
                    return func(*args, **kwargs)
                except Exception:
                    _success = False
                    raise
                finally:
                    elapsed_ms = (time.perf_counter() - _t0) * 1000
                    lithos_metrics.knowledge_write_duration.record(
                        elapsed_ms, {"op": op, "success": _success}
                    )

            return sync_wrapper  # type: ignore[return-value]

    return decorator  # type: ignore[return-value]


# --- @tool_metrics decorator ---


def tool_metrics(
    tool_name: str | None = None,
) -> Callable[[F], F]:
    """Decorator that increments per-tool call and error counters.

    Increments ``lithos.tool.calls`` on every invocation and
    ``lithos.tool.errors`` whenever the handler raises an exception.

    Works for both sync and async functions. When OTEL is disabled the
    counters are no-ops so there is no observable overhead.

    Args:
        tool_name: Explicit tool name. Defaults to ``func.__name__``.

    Usage::

        @self.mcp.tool()
        @tool_metrics()
        async def lithos_write(...):
            ...

        # Or with an explicit name:
        @tool_metrics("lithos_search")
        async def lithos_search(...):
            ...
    """

    def decorator(func: F) -> F:
        name = tool_name or func.__name__

        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                lithos_metrics.tool_calls.add(1, {"tool_name": name})
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    lithos_metrics.tool_errors.add(
                        1, {"tool_name": name, "error_type": type(exc).__name__}
                    )
                    raise

            return async_wrapper  # type: ignore[return-value]
        else:

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                lithos_metrics.tool_calls.add(1, {"tool_name": name})
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    lithos_metrics.tool_errors.add(
                        1, {"tool_name": name, "error_type": type(exc).__name__}
                    )
                    raise

            return sync_wrapper  # type: ignore[return-value]

    return decorator


# --- Lazy metric instruments ---


class _LithosMetrics:
    """Lazy metric instruments. Created on first access after setup_telemetry()."""

    def __init__(self) -> None:
        self._knowledge_ops: Any = None
        self._knowledge_write_duration: Any = None
        self._search_ops: Any = None
        self._search_duration: Any = None
        self._coordination_ops: Any = None
        self._event_bus_ops: Any = None
        self._event_bus_subscriber_drops: Any = None
        self._cache_lookups: Any = None
        self._cache_lookup_duration: Any = None
        self._reconcile_ops: Any = None
        self._startup_duration: Any = None
        self._file_watcher_events: Any = None
        self._tool_calls: Any = None
        self._tool_errors: Any = None
        self._sse_events_delivered: Any = None
        # LCMA metrics
        self._lcma_enrich_queue_processing_lag: Any = None
        self._lcma_enrich_queue_attempts: Any = None
        self._lcma_enrich_exhausted: Any = None
        self._lcma_retrieve_duration: Any = None
        self._lcma_retrieve_candidates_considered: Any = None
        self._lcma_retrieve_final_nodes: Any = None
        self._lcma_temperature_cold_start: Any = None
        self._lcma_scout_duration: Any = None
        self._lcma_scout_candidates: Any = None
        self._lcma_salience_updates: Any = None

    @property
    def knowledge_ops(self) -> Any:
        if self._knowledge_ops is None:
            self._knowledge_ops = get_meter().create_counter(
                "lithos.knowledge.operations",
                description="Knowledge CRUD operations",
            )
        return self._knowledge_ops

    @property
    def knowledge_write_duration(self) -> Any:
        """Histogram tracking create/update latency in milliseconds.

        Attributes:
            op:      "create" | "update"
            success: bool (True = no exception raised)
        """
        if self._knowledge_write_duration is None:
            self._knowledge_write_duration = get_meter().create_histogram(
                "lithos.knowledge.write_duration_ms",
                description="Knowledge write (create/update) latency in milliseconds",
                unit="ms",
            )
        return self._knowledge_write_duration

    @property
    def search_ops(self) -> Any:
        if self._search_ops is None:
            self._search_ops = get_meter().create_counter(
                "lithos.search.operations",
                description="Search operations",
            )
        return self._search_ops

    @property
    def search_duration(self) -> Any:
        if self._search_duration is None:
            self._search_duration = get_meter().create_histogram(
                "lithos.search.duration_ms",
                description="Search latency in milliseconds",
            )
        return self._search_duration

    @property
    def coordination_ops(self) -> Any:
        if self._coordination_ops is None:
            self._coordination_ops = get_meter().create_counter(
                "lithos.coordination.operations",
                description="Coordination operations",
            )
        return self._coordination_ops

    @property
    def event_bus_ops(self) -> Any:
        if self._event_bus_ops is None:
            self._event_bus_ops = get_meter().create_counter(
                "lithos.event_bus.operations",
                description="Event bus emit and drop operations",
            )
        return self._event_bus_ops

    @property
    def event_bus_subscriber_drops(self) -> Any:
        """Counter incremented when a subscriber queue is full and an event is dropped.

        Attributes:
            subscriber_id: UUID identifying the subscriber whose queue was full.
        """
        if self._event_bus_subscriber_drops is None:
            self._event_bus_subscriber_drops = get_meter().create_counter(
                "lithos.event_bus.subscriber_drops",
                description="Events dropped because a subscriber queue was full",
            )
        return self._event_bus_subscriber_drops

    @property
    def cache_lookups(self) -> Any:
        if self._cache_lookups is None:
            self._cache_lookups = get_meter().create_counter(
                "lithos.cache.lookups",
                description="Cache lookup operations",
            )
        return self._cache_lookups

    @property
    def cache_lookup_duration(self) -> Any:
        if self._cache_lookup_duration is None:
            self._cache_lookup_duration = get_meter().create_histogram(
                "lithos.cache.lookup_duration_ms",
                description="Cache lookup latency in milliseconds",
            )
        return self._cache_lookup_duration

    @property
    def reconcile_ops(self) -> Any:
        if self._reconcile_ops is None:
            self._reconcile_ops = get_meter().create_counter(
                "lithos.reconcile.operations",
                description="Reconcile scope operations by scope and status",
            )
        return self._reconcile_ops

    @property
    def startup_duration(self) -> Any:
        """Histogram tracking server startup duration in milliseconds.

        Recorded once per process startup, from the start of ``initialize()``
        to the point when all components are ready.
        """
        if self._startup_duration is None:
            self._startup_duration = get_meter().create_histogram(
                "lithos.startup_duration",
                description="Server startup duration from initialize() start to ready",
                unit="ms",
            )
        return self._startup_duration

    @property
    def file_watcher_events(self) -> Any:
        """Counter tracking file watcher events by event type.

        Attributes:
            event_type: "created" | "updated" | "deleted"
        """
        if self._file_watcher_events is None:
            self._file_watcher_events = get_meter().create_counter(
                "lithos.file_watcher.events_total",
                description="Total file watcher events by event type",
            )
        return self._file_watcher_events

    @property
    def tool_calls(self) -> Any:
        """Counter incremented on every MCP tool invocation.

        Attributes:
            tool_name: The name of the MCP tool (e.g. ``"lithos_write"``).
        """
        if self._tool_calls is None:
            self._tool_calls = get_meter().create_counter(
                "lithos.tool.calls",
                description="MCP tool invocation count",
            )
        return self._tool_calls

    @property
    def tool_errors(self) -> Any:
        """Counter incremented whenever a tool handler raises an exception.

        Attributes:
            tool_name:  The name of the MCP tool.
            error_type: The exception class name (e.g. ``"ValueError"``).
        """
        if self._tool_errors is None:
            self._tool_errors = get_meter().create_counter(
                "lithos.tool.errors",
                description="MCP tool error count",
            )
        return self._tool_errors

    @property
    def sse_events_delivered(self) -> Any:
        """Counter tracking total SSE events successfully delivered to clients."""
        if self._sse_events_delivered is None:
            self._sse_events_delivered = get_meter().create_counter(
                "lithos.sse.events_delivered",
                description="Total SSE events successfully delivered to connected clients",
                unit="events",
            )
        return self._sse_events_delivered

    # ------------------------------------------------------------------
    # LCMA metrics
    # ------------------------------------------------------------------

    @property
    def lcma_enrich_queue_processing_lag(self) -> Any:
        """Histogram tracking time from triggered_at to processed_at per enrich_queue item.

        Attributes:
            (none — item-level latency, ms)
        """
        if self._lcma_enrich_queue_processing_lag is None:
            self._lcma_enrich_queue_processing_lag = get_meter().create_histogram(
                "lithos.lcma.enrich_queue.processing_lag_ms",
                description="Time from triggered_at to processed_at per enrich_queue item",
                unit="ms",
            )
        return self._lcma_enrich_queue_processing_lag

    @property
    def lcma_enrich_queue_attempts(self) -> Any:
        """Histogram tracking attempt count per enrich_queue item at completion."""
        if self._lcma_enrich_queue_attempts is None:
            self._lcma_enrich_queue_attempts = get_meter().create_histogram(
                "lithos.lcma.enrich_queue.attempts",
                description="Attempt count per enrich_queue item at completion (success or exhausted)",
            )
        return self._lcma_enrich_queue_attempts

    @property
    def lcma_enrich_exhausted(self) -> Any:
        """Counter incremented when an enrich_queue item hits max attempts and is abandoned."""
        if self._lcma_enrich_exhausted is None:
            self._lcma_enrich_exhausted = get_meter().create_counter(
                "lithos.lcma.enrich.exhausted",
                description="Enrich queue items abandoned after exhausting max retry attempts",
            )
        return self._lcma_enrich_exhausted

    @property
    def lcma_retrieve_duration(self) -> Any:
        """Histogram tracking end-to-end run_retrieve latency in milliseconds."""
        if self._lcma_retrieve_duration is None:
            self._lcma_retrieve_duration = get_meter().create_histogram(
                "lithos.lcma.retrieve.duration_ms",
                description="End-to-end run_retrieve latency in milliseconds",
                unit="ms",
            )
        return self._lcma_retrieve_duration

    @property
    def lcma_retrieve_candidates_considered(self) -> Any:
        """Histogram tracking candidates_considered value per run_retrieve call."""
        if self._lcma_retrieve_candidates_considered is None:
            self._lcma_retrieve_candidates_considered = get_meter().create_histogram(
                "lithos.lcma.retrieve.candidates_considered",
                description="Number of candidates considered per run_retrieve call",
            )
        return self._lcma_retrieve_candidates_considered

    @property
    def lcma_retrieve_final_nodes(self) -> Any:
        """Histogram tracking number of final result nodes returned per run_retrieve call."""
        if self._lcma_retrieve_final_nodes is None:
            self._lcma_retrieve_final_nodes = get_meter().create_histogram(
                "lithos.lcma.retrieve.final_nodes",
                description="Number of final result nodes returned per run_retrieve call",
            )
        return self._lcma_retrieve_final_nodes

    @property
    def lcma_temperature_cold_start(self) -> Any:
        """Counter incremented when compute_temperature returns a cold-start result."""
        if self._lcma_temperature_cold_start is None:
            self._lcma_temperature_cold_start = get_meter().create_counter(
                "lithos.lcma.temperature.cold_start",
                description="Number of cold-start temperature results from compute_temperature",
            )
        return self._lcma_temperature_cold_start

    @property
    def lcma_scout_duration(self) -> Any:
        """Histogram tracking per-scout invocation duration in milliseconds.

        Attributes:
            scout: the scout name (e.g. ``"scout_vector"``)
        """
        if self._lcma_scout_duration is None:
            self._lcma_scout_duration = get_meter().create_histogram(
                "lithos.lcma.scout.duration_ms",
                description="Time taken per scout invocation in milliseconds",
                unit="ms",
            )
        return self._lcma_scout_duration

    @property
    def lcma_scout_candidates(self) -> Any:
        """Histogram tracking candidates returned per scout invocation.

        Attributes:
            scout: the scout name (e.g. ``"scout_vector"``)
        """
        if self._lcma_scout_candidates is None:
            self._lcma_scout_candidates = get_meter().create_histogram(
                "lithos.lcma.scout.candidates",
                description="Number of candidates returned per scout invocation",
            )
        return self._lcma_scout_candidates

    @property
    def lcma_salience_updates(self) -> Any:
        """Counter incremented on each update_salience call."""
        if self._lcma_salience_updates is None:
            self._lcma_salience_updates = get_meter().create_counter(
                "lithos.lcma.salience.updates",
                description="Total update_salience calls",
            )
        return self._lcma_salience_updates


def register_active_claims_observer(get_active_claim_count: Callable[[], int]) -> None:
    """Register an observable gauge backed by real DB state."""
    meter = get_meter()
    meter.create_observable_gauge(
        "lithos.coordination.active_claims",
        callbacks=[lambda _options: [Observation(int(get_active_claim_count()))]],
        description="Currently active non-expired task claims",
    )


_sse_active_clients_gauge_registered: bool = False


def register_sse_active_clients_observer(get_client_count: Callable[[], int]) -> None:
    """Register an observable gauge for the number of currently connected SSE clients.

    The callback must be **synchronous** and cheap — it runs inside the OTEL SDK
    metric collection loop.  Pass a lambda that reads a cached integer counter.

    Idempotent: calling this more than once is a no-op (the gauge is registered
    at most once per process lifetime).

    Gauge registered:
        ``lithos.sse.active_clients`` — number of currently connected SSE clients.
    """
    global _sse_active_clients_gauge_registered
    if _sse_active_clients_gauge_registered:
        return
    _sse_active_clients_gauge_registered = True
    meter = get_meter()
    meter.create_observable_gauge(
        "lithos.sse.active_clients",
        callbacks=[lambda _options: [Observation(int(get_client_count()))]],
        description="Number of currently connected SSE clients",
    )


def register_resource_gauges(
    *,
    get_document_count: Callable[[], int],
    get_stale_document_count: Callable[[], int],
    get_tantivy_document_count: Callable[[], int],
    get_chroma_chunk_count: Callable[[], int],
    get_graph_node_count: Callable[[], int],
    get_graph_edge_count: Callable[[], int],
    get_agent_count: Callable[[], int],
) -> None:
    """Register OTEL observable gauges for all resource-level metrics.

    All callbacks must be **synchronous** and cheap to call — they run inside
    the OTEL SDK metric collection loop.  Use cached values for anything backed
    by async I/O.

    Gauges registered:
        - ``lithos.knowledge.document_count``
        - ``lithos.knowledge.stale_document_count``
        - ``lithos.search.tantivy_document_count``
        - ``lithos.search.chroma_chunk_count``
        - ``lithos.graph.node_count``
        - ``lithos.graph.edge_count``
        - ``lithos.agents.active_count``
    """
    meter = get_meter()

    meter.create_observable_gauge(
        "lithos.knowledge.document_count",
        callbacks=[lambda _: [Observation(int(get_document_count()))]],
        description="Total number of documents in the knowledge store",
    )
    meter.create_observable_gauge(
        "lithos.knowledge.stale_document_count",
        callbacks=[lambda _: [Observation(int(get_stale_document_count()))]],
        description="Documents whose expires_at is set and in the past",
    )
    meter.create_observable_gauge(
        "lithos.search.tantivy_document_count",
        callbacks=[lambda _: [Observation(int(get_tantivy_document_count()))]],
        description="Number of documents indexed in Tantivy (full-text)",
    )
    meter.create_observable_gauge(
        "lithos.search.chroma_chunk_count",
        callbacks=[lambda _: [Observation(int(get_chroma_chunk_count()))]],
        description="Number of embedding chunks in ChromaDB",
    )
    meter.create_observable_gauge(
        "lithos.graph.node_count",
        callbacks=[lambda _: [Observation(int(get_graph_node_count()))]],
        description="Number of nodes in the wiki-link knowledge graph",
    )
    meter.create_observable_gauge(
        "lithos.graph.edge_count",
        callbacks=[lambda _: [Observation(int(get_graph_edge_count()))]],
        description="Number of edges in the wiki-link knowledge graph",
    )
    meter.create_observable_gauge(
        "lithos.agents.active_count",
        callbacks=[lambda _: [Observation(int(get_agent_count()))]],
        description="Number of agents registered in the coordination store",
    )


def register_event_bus_metrics(event_bus: Any) -> None:
    """Register OTEL observable gauge for event bus buffer utilisation.

    Registers ``lithos.event_bus.buffer_utilisation`` — an observable gauge
    that emits one ``Observation`` per subscriber, in the range [0.0, 1.0],
    representing the current queue fill fraction.

    The gauge is per-subscriber, tagged with ``subscriber_id``.

    Safe to call even if OTEL is not active — in that case it is a no-op.
    Idempotent: subsequent calls after the first are silently ignored to
    prevent duplicate gauge registration when multiple ``EventBus`` instances
    are created (e.g. in tests).

    Args:
        event_bus: The ``EventBus`` instance whose subscribers to observe.
    """
    global _event_bus_metrics_registered
    if _event_bus_metrics_registered:
        return
    _event_bus_metrics_registered = True

    if not (_HAS_OTEL and _initialized):
        return

    meter = get_meter()

    def _buffer_utilisation_callback(_options: Any) -> list[Any]:
        return [
            Observation(ratio, {"subscriber_id": sub_id})
            for sub_id, ratio in event_bus.get_buffer_utilisation()
        ]

    meter.create_observable_gauge(
        "lithos.event_bus.buffer_utilisation",
        callbacks=[_buffer_utilisation_callback],
        description=("Current fill ratio of each subscriber queue (0.0 = empty, 1.0 = full)"),
    )


_lcma_metrics_registered: bool = False


def register_lcma_metrics(
    *,
    get_enrich_queue_depth: Callable[[], int],
    get_coactivation_pairs: Callable[[], int],
    get_working_memory_active_tasks: Callable[[], int],
) -> None:
    """Register OTEL observable gauges for LCMA pipeline metrics.

    Registers three observable gauges backed by live DB state:
        - ``lithos.lcma.enrich_queue.depth`` — current queue depth (unprocessed items).
        - ``lithos.lcma.coactivation.pairs`` — total rows in the coactivation table.
        - ``lithos.lcma.working_memory.active_tasks`` — distinct task_ids with recent activity.

    All callbacks must be **synchronous** and cheap — they run inside the OTEL SDK
    metric collection loop. Pass lambdas that read from cached integer counters.

    Idempotent: calling this more than once is a no-op.

    Safe to call even when OTEL is not active — in that case it is a no-op.

    Args:
        get_enrich_queue_depth: Returns current unprocessed enrich_queue item count.
        get_coactivation_pairs: Returns total rows in the coactivation table.
        get_working_memory_active_tasks: Returns count of distinct task_ids with
            last_seen_at within the last 24 hours.
    """
    global _lcma_metrics_registered
    if _lcma_metrics_registered:
        return
    _lcma_metrics_registered = True

    if not (_HAS_OTEL and _initialized):
        return

    meter = get_meter()

    meter.create_observable_gauge(
        "lithos.lcma.enrich_queue.depth",
        callbacks=[lambda _: [Observation(int(get_enrich_queue_depth()))]],
        description="Current number of unprocessed items in the enrich_queue table",
    )
    meter.create_observable_gauge(
        "lithos.lcma.coactivation.pairs",
        callbacks=[lambda _: [Observation(int(get_coactivation_pairs()))]],
        description="Total number of rows in the coactivation table",
    )
    meter.create_observable_gauge(
        "lithos.lcma.working_memory.active_tasks",
        callbacks=[lambda _: [Observation(int(get_working_memory_active_tasks()))]],
        description="Count of distinct task_ids in working_memory with activity in the last 24 hours",
    )


lithos_metrics = _LithosMetrics()


# --- Test helpers ---


def _reset_for_testing() -> None:
    """Reset module state. For tests only."""
    global \
        _initialized, \
        _tracer_provider, \
        _meter_provider, \
        _log_provider, \
        _trace_context_filter, \
        _event_bus_metrics_registered, \
        _lcma_metrics_registered
    _initialized = False
    _event_bus_metrics_registered = False
    _lcma_metrics_registered = False
    _tracer_provider = None
    _meter_provider = None
    _log_provider = None
    if _trace_context_filter is not None:
        logging.getLogger().removeFilter(_trace_context_filter)
        _trace_context_filter = None

    # Reset global OTEL providers so set_tracer_provider() can be called again
    if _HAS_OTEL:
        from opentelemetry.util._once import Once

        trace._TRACER_PROVIDER_SET_ONCE = Once()
        trace._TRACER_PROVIDER = None
        metrics._METER_PROVIDER_SET_ONCE = Once()  # type: ignore[attr-defined]
        metrics._METER_PROVIDER = None  # type: ignore[attr-defined]
