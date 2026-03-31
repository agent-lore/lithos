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
        self._cache_lookups: Any = None
        self._cache_lookup_duration: Any = None
        self._reconcile_ops: Any = None

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


def register_active_claims_observer(get_active_claim_count: Callable[[], int]) -> None:
    """Register an observable gauge backed by real DB state."""
    meter = get_meter()
    meter.create_observable_gauge(
        "lithos.coordination.active_claims",
        callbacks=[lambda _options: [Observation(int(get_active_claim_count()))]],
        description="Currently active non-expired task claims",
    )


lithos_metrics = _LithosMetrics()


# --- Test helpers ---


def _reset_for_testing() -> None:
    """Reset module state. For tests only."""
    global _initialized, _tracer_provider, _meter_provider, _log_provider, _trace_context_filter
    _initialized = False
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
