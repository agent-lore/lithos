# OTEL Instrumentation Plan for Lithos

## Prerequisites

**Local OTEL stack:** The Collector, Prometheus, Tempo, Loki, Grafana, and Opik infrastructure lives in the [lithos-observability](https://github.com/your-org/lithos-observability) repo. Start it with `docker compose up -d` before enabling telemetry in Lithos.

**Key principles:**

- OTEL is **opt-in** -- `LITHOS_TELEMETRY__ENABLED=true` enables it, otherwise minimal overhead
- OTEL is **additive** -- `docker logs lithos` works exactly as before
- OTEL packages are **optional** -- `uv sync --extra otel` installs them; Lithos runs fine without
- **Console fallback** -- set `LITHOS_TELEMETRY__CONSOLE_FALLBACK=true` without an OTLP endpoint and spans print to stdout (useful for dev without the full stack)

---

## Phase 1: Foundation

### Step 1: Add Optional Dependencies (pyproject.toml)

Add an `otel` optional dependency group alongside the existing `dev` group:

```toml
[project.optional-dependencies]
dev = [
    "pytest>=7.0.0",
    "pytest-asyncio>=0.21.0",
    "pytest-cov>=4.0.0",
    "ruff>=0.1.0",
]
otel = [
    "opentelemetry-sdk>=1.28.0",
    "opentelemetry-api>=1.28.0",
    "opentelemetry-exporter-otlp-proto-http>=1.28.0",
]
```

Install with:

```bash
uv sync --extra otel
```

### Step 2: Add TelemetryConfig (src/lithos/config.py)

Add a `TelemetryConfig` nested model to `LithosConfig`, following the same pattern as `ServerConfig`, `StorageConfig`, etc.:

```python
class TelemetryConfig(BaseModel):
    """OpenTelemetry configuration."""

    enabled: bool = False
    endpoint: str | None = None  # OTLP HTTP endpoint, e.g. "http://otel-collector:4318"
    console_fallback: bool = False  # Print spans to stdout when no endpoint
    service_name: str = "lithos"
    export_interval_ms: int = 30_000  # Metrics export interval
```

Add to `LithosConfig`:

```python
class LithosConfig(BaseSettings):
    # ... existing fields ...
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
```

Add backward-compatible env var handling in `load_config()`:

```python
# Backward compat: LITHOS_OTEL_ENABLED and OTEL_EXPORTER_OTLP_ENDPOINT
env_otel_enabled = os.environ.get("LITHOS_OTEL_ENABLED")
if env_otel_enabled:
    config.telemetry.enabled = env_otel_enabled.lower() in ("1", "true")

env_otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
if env_otlp_endpoint:
    config.telemetry.endpoint = env_otlp_endpoint
```

This allows both the pydantic-native `LITHOS_TELEMETRY__ENABLED=true` and the docker-compose-friendly `LITHOS_OTEL_ENABLED=true` patterns.

Endpoint contract: `telemetry.endpoint` should be a base OTLP HTTP collector URL (example: `http://otel-collector:4318`). The code also honors `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` / `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` when signal-specific endpoints are needed.

### Step 3: Create src/lithos/telemetry.py

This is the core module. It handles three scenarios:

1. OTEL packages installed, telemetry enabled -- full instrumentation
2. OTEL packages installed, telemetry disabled -- no-ops, minimal overhead
3. OTEL packages **not installed** -- no-ops, minimal overhead, no import errors

```python
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
from typing import TYPE_CHECKING, Any, Callable, TypeVar

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

    _HAS_OTEL = True
except ImportError:
    _HAS_OTEL = False


# --- Module state ---

_initialized = False
_tracer_provider: Any = None  # TracerProvider when active
_meter_provider: Any = None  # MeterProvider when active


# --- No-op stubs for when OTEL is absent or disabled ---


class _NoOpSpan:
    """Minimal no-op span."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, status: Any) -> None:
        pass

    def record_exception(self, exc: Exception) -> None:
        pass

    def __enter__(self) -> "_NoOpSpan":
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
    global _initialized, _tracer_provider, _meter_provider

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

    resource = Resource.create({
        "service.name": config.telemetry.service_name,
        "service.version": service_version,
    })

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
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        span_exporter = OTLPSpanExporter(endpoint=traces_endpoint)
        _tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    elif config.telemetry.console_fallback:
        _tracer_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(_tracer_provider)

    # --- Metrics ---
    metric_reader = None

    if metrics_endpoint:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

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

    _initialized = True
    logger.info("OpenTelemetry initialized (endpoint=%s)", endpoint)


def shutdown_telemetry() -> None:
    """Flush and shut down OTEL providers. Call on exit.

    Ensures in-flight spans are exported before the process terminates.
    Safe to call if telemetry was never initialized.
    """
    global _initialized, _tracer_provider, _meter_provider

    if not _initialized:
        return

    if _tracer_provider is not None:
        _tracer_provider.force_flush()
        _tracer_provider.shutdown()
        _tracer_provider = None

    if _meter_provider is not None:
        _meter_provider.shutdown()
        _meter_provider = None

    _initialized = False
    logger.debug("OpenTelemetry shut down")


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


# --- Lazy metric instruments ---


class _LithosMetrics:
    """Lazy metric instruments. Created on first access after setup_telemetry()."""

    def __init__(self) -> None:
        self._knowledge_ops: Any = None
        self._search_ops: Any = None
        self._search_duration: Any = None
        self._coordination_ops: Any = None

    @property
    def knowledge_ops(self) -> Any:
        if self._knowledge_ops is None:
            self._knowledge_ops = get_meter().create_counter(
                "lithos.knowledge.operations",
                description="Knowledge CRUD operations",
            )
        return self._knowledge_ops

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
    global _initialized, _tracer_provider, _meter_provider
    _initialized = False
    _tracer_provider = None
    _meter_provider = None
```

### Step 4: Wire Lifecycle into CLI (src/lithos/cli.py)

In the `serve` command, add telemetry setup before server creation and shutdown on exit:

```python
@cli.command()
@click.pass_context
def serve(ctx, transport, host, port, watch):
    """Start the Lithos MCP server."""
    from lithos.server import create_server
    from lithos.telemetry import setup_telemetry, shutdown_telemetry

    config: LithosConfig = ctx.obj["config"]

    # Initialize telemetry before anything else
    setup_telemetry(config)

    server = create_server(config)

    async def run_server() -> None:
        click.echo("Initializing Lithos...")
        await server.initialize()

        if watch:
            click.echo("Starting file watcher...")
            server.start_file_watcher()

        click.echo(f"Starting MCP server ({transport} transport)...")

        if transport == "stdio":
            await server.mcp.run_stdio_async(show_banner=False)
        else:
            click.echo(f"Listening on http://{host}:{port}")
            await server.mcp.run_http_async(
                transport="sse", host=host, port=port,
                path="/sse", show_banner=False,
            )

    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        click.echo("\nShutting down...")
        server.stop_file_watcher()
    finally:
        shutdown_telemetry()  # Flush all pending spans/metrics
```

The `finally` block ensures spans are flushed regardless of how the server exits.

---

## Phase 2: Instrument

### Step 1: Replace print() with Logging (src/lithos/server.py)

Three `print()` calls need migration to `logging.getLogger(__name__)`:

```python
import logging
logger = logging.getLogger(__name__)

# Line 86:  print(f"Error indexing {file_path}: {e}")
#        -> logger.error("Error indexing %s: %s", file_path, e)

# Line 142: print(f"Error handling file change {path}: {e}")
#        -> logger.error("Error handling file change %s: %s", path, e)

# Line 810: print(f"Error processing file update: {exception}")
#        -> logger.error("Error processing file update: %s", exception)
```

This follows the same pattern already used in `search.py`.

### Step 2: Multi-Level Instrumentation

Instead of only instrumenting at the server.py tool level, add `@traced` at multiple levels to produce rich trace trees.

**search.py** -- where real search latency lives:

```python
import time

from lithos.telemetry import traced, lithos_metrics

class SearchEngine:

    @traced("lithos.search.fulltext")
    def full_text_search(self, query, limit=10, tags=None, author=None, path_prefix=None):
        start = time.perf_counter()
        success = False
        try:
            # ... existing code ...
            lithos_metrics.search_ops.add(1, {"type": "fulltext"})
            success = True
            return results
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            lithos_metrics.search_duration.record(
                elapsed_ms,
                {"type": "fulltext", "success": success},
            )

    @traced("lithos.search.semantic")
    def semantic_search(self, query, limit=10, threshold=None, tags=None):
        start = time.perf_counter()
        success = False
        try:
            # ... existing code ...
            lithos_metrics.search_ops.add(1, {"type": "semantic"})
            success = True
            return results
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            lithos_metrics.search_duration.record(
                elapsed_ms,
                {"type": "semantic", "success": success},
            )

    @traced("lithos.search.index_document")
    def index_document(self, doc):
        # ... existing code ...

    @traced("lithos.search.remove_document")
    def remove_document(self, doc_id):
        # ... existing code ...
```

**coordination.py** -- SQLite operations:

```python
from lithos.telemetry import traced, lithos_metrics

class CoordinationService:

    @traced("lithos.coordination.create_task")
    async def create_task(self, title, agent, description=None, tags=None):
        # ... existing code ...
        lithos_metrics.coordination_ops.add(1, {"op": "create_task"})

    @traced("lithos.coordination.claim_task")
    async def claim_task(self, task_id, aspect, agent, ttl_minutes=60):
        # ... existing code ...
        lithos_metrics.coordination_ops.add(1, {"op": "claim"})

    @traced("lithos.coordination.release_claim")
    async def release_claim(self, task_id, aspect, agent):
        # ... existing code ...

    @traced("lithos.coordination.complete_task")
    async def complete_task(self, task_id, agent):
        # ... existing code ...
        lithos_metrics.coordination_ops.add(1, {"op": "complete"})
```

For `active_claims`, do **not** increment/decrement in request paths (it drifts on expiry, renewals, and failures). Use the observable gauge registered in `setup_telemetry()` with a callback that reads current active claims from SQLite.

**knowledge.py** -- file I/O:

```python
from lithos.telemetry import traced, lithos_metrics

class KnowledgeManager:

    @traced("lithos.knowledge.create")
    async def create(self, title, content, agent, ...):
        # ... existing code ...
        lithos_metrics.knowledge_ops.add(1, {"op": "create"})

    @traced("lithos.knowledge.read")
    async def read(self, id=None, path=None, max_length=None):
        # ... existing code ...
        lithos_metrics.knowledge_ops.add(1, {"op": "read"})

    @traced("lithos.knowledge.update")
    async def update(self, id, agent, content=None, ...):
        # ... existing code ...
        lithos_metrics.knowledge_ops.add(1, {"op": "update"})

    @traced("lithos.knowledge.delete")
    async def delete(self, id):
        # ... existing code ...
        lithos_metrics.knowledge_ops.add(1, {"op": "delete"})
```

**server.py** -- top-level MCP tool spans with request attributes:

For MCP tool functions (closures inside `_register_tools()`), use `get_tracer()` directly to add tool-level attributes. The `@traced` calls on the underlying modules automatically appear as child spans:

```python
import hashlib

from lithos.telemetry import get_tracer

@self.mcp.tool()
async def lithos_search(query: str, limit: int = 10, ...) -> dict:
    tracer = get_tracer()
    with tracer.start_as_current_span("lithos.tool.search") as span:
        span.set_attribute("lithos.tool", "lithos_search")
        span.set_attribute("lithos.query.length", len(query))
        span.set_attribute("lithos.query.sha256", hashlib.sha256(query.encode()).hexdigest()[:16])
        span.set_attribute("lithos.limit", limit)
        results = self.search.full_text_search(...)  # produces child span
        span.set_attribute("lithos.result_count", len(results))
        return {"results": [...]}

@self.mcp.tool()
async def lithos_write(title, content, agent, ...) -> dict:
    tracer = get_tracer()
    with tracer.start_as_current_span("lithos.tool.write") as span:
        span.set_attribute("lithos.tool", "lithos_write")
        span.set_attribute("lithos.agent", agent)
        # Each call produces a child span:
        await self.coordination.ensure_agent_known(agent)
        doc = await self.knowledge.create(...)
        self.search.index_document(doc)
        self.graph.add_document(doc)
        span.set_attribute("lithos.doc_id", doc.id)
        return {"id": doc.id, "path": str(doc.path)}
```

Avoid putting raw query text, document content, prompts, or completions into span attributes/events by default.

This produces trace trees like:

```text
lithos.tool.write (server.py)           120ms
  lithos.coordination.ensure_agent (coordination.py)  2ms
  lithos.knowledge.create (knowledge.py)              8ms
  lithos.search.index_document (search.py)          105ms
```

Also instrument the initialization path:

```python
async def _rebuild_indices(self) -> None:
    tracer = get_tracer()
    with tracer.start_as_current_span("lithos.index.rebuild") as span:
        # ... existing code ...
        span.set_attribute("lithos.file_count", count)
        span.set_attribute("lithos.error_count", errors)
```

### Step 3: Metrics Summary

All metrics are accessed via the lazy `lithos_metrics` singleton (see telemetry.py above). Summary of instruments:

| Metric | Type | Description |
| ------ | ---- | ----------- |
| `lithos.knowledge.operations` | Counter | Knowledge CRUD ops, attr: `op` |
| `lithos.search.operations` | Counter | Search ops, attr: `type` (fulltext/semantic) |
| `lithos.search.duration_ms` | Histogram | Search latency (attrs: `type`, `success`) |
| `lithos.coordination.operations` | Counter | Coordination ops, attr: `op` |
| `lithos.coordination.active_claims` | ObservableGauge | Current non-expired claims (DB-derived) |

---

## Phase 3: LLM Tracing (Future)

When LLM calls are added to Lithos (Cognitive Memory), add OpenLIT for auto-instrumentation:

```python
# In setup_telemetry(), after OTEL init:
if os.getenv("LITHOS_LLM_TRACING", "").lower() in ("1", "true"):
    import openlit
    openlit.init()  # auto-instruments OpenAI, Anthropic, LiteLLM, etc.
    # LLM spans appear in the SAME trace with gen_ai.* attributes
    # (model, tokens, cost, prompt, completion)
```

Add OpenLIT as an optional dependency:

```toml
[project.optional-dependencies]
llm = [
    "openlit>=1.0.0",
]
```

For the Opik LLM analysis UI, see the lithos-observability repo (`docker-compose.opik.yml`).

---

## Testing (tests/test_telemetry.py)

```python
"""Tests for OpenTelemetry instrumentation."""

import asyncio

import pytest


def _has_otel_packages() -> bool:
    try:
        import opentelemetry  # noqa: F401

        return True
    except ImportError:
        return False


class TestTelemetryDisabled:
    """Tests for when telemetry is disabled (default)."""

    def test_setup_noop_when_disabled(self, test_config):
        from lithos.telemetry import _initialized, _reset_for_testing, setup_telemetry

        _reset_for_testing()
        setup_telemetry(test_config)  # enabled=False by default
        assert not _initialized
        _reset_for_testing()

    def test_get_tracer_returns_noop(self):
        from lithos.telemetry import _NoOpTracer, _reset_for_testing, get_tracer

        _reset_for_testing()
        tracer = get_tracer()
        assert isinstance(tracer, _NoOpTracer)

    def test_traced_decorator_passthrough(self):
        from lithos.telemetry import traced

        @traced("test.span")
        def my_func(x):
            return x * 2

        assert my_func(5) == 10

    def test_traced_async_decorator_passthrough(self):
        from lithos.telemetry import traced

        @traced("test.async_span")
        async def my_async_func(x):
            return x * 3

        result = asyncio.run(my_async_func(5))
        assert result == 15

    def test_noop_metrics_dont_raise(self):
        from lithos.telemetry import _reset_for_testing, lithos_metrics

        _reset_for_testing()
        lithos_metrics.knowledge_ops.add(1, {"op": "create"})
        lithos_metrics.search_duration.record(42.0)
        # should not raise


@pytest.mark.skipif(
    not _has_otel_packages(),
    reason="opentelemetry-sdk not installed",
)
class TestTelemetryEnabled:
    """Tests for when telemetry is enabled (requires otel extra)."""

    @pytest.fixture
    def otel_setup(self, test_config):
        from opentelemetry.sdk.trace.export.in_memory import InMemorySpanExporter

        from lithos.telemetry import _reset_for_testing, setup_telemetry, shutdown_telemetry

        _reset_for_testing()
        test_config.telemetry.enabled = True
        exporter = InMemorySpanExporter()
        setup_telemetry(test_config, _test_span_exporter=exporter)
        yield exporter
        shutdown_telemetry()
        _reset_for_testing()

    def test_traced_creates_span(self, otel_setup):
        from lithos.telemetry import traced

        @traced("test.operation")
        def do_something():
            return 42

        result = do_something()
        assert result == 42

        spans = otel_setup.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "test.operation"

    def test_traced_records_exception(self, otel_setup):
        from lithos.telemetry import traced

        @traced("test.failing")
        def do_fail():
            raise ValueError("boom")

        with pytest.raises(ValueError):
            do_fail()

        spans = otel_setup.get_finished_spans()
        assert len(spans) == 1
        assert not spans[0].status.is_ok

    def test_nested_spans_create_trace_tree(self, otel_setup):
        from lithos.telemetry import traced

        @traced("parent")
        def parent():
            return child()

        @traced("child")
        def child():
            return "done"

        parent()
        spans = otel_setup.get_finished_spans()
        assert len(spans) == 2
        child_span = next(s for s in spans if s.name == "child")
        parent_span = next(s for s in spans if s.name == "parent")
        assert child_span.parent.span_id == parent_span.context.span_id

    def test_shutdown_flushes_spans(self, test_config):
        from opentelemetry.sdk.trace.export.in_memory import InMemorySpanExporter

        from lithos.telemetry import (
            _reset_for_testing,
            get_tracer,
            setup_telemetry,
            shutdown_telemetry,
        )

        _reset_for_testing()
        test_config.telemetry.enabled = True
        exporter = InMemorySpanExporter()
        setup_telemetry(test_config, _test_span_exporter=exporter)

        with get_tracer().start_as_current_span("flush.test"):
            pass

        shutdown_telemetry()
        assert len(exporter.get_finished_spans()) == 1
        _reset_for_testing()
```

Also add one CLI lifecycle integration test:

```python
def test_serve_shutdown_calls_telemetry_shutdown(monkeypatch, test_config):
    calls = {"setup": 0, "shutdown": 0}
    monkeypatch.setattr("lithos.telemetry.setup_telemetry", lambda cfg: calls.__setitem__("setup", 1))
    monkeypatch.setattr("lithos.telemetry.shutdown_telemetry", lambda: calls.__setitem__("shutdown", 1))
    # invoke serve() in a controlled test harness and assert shutdown always executes
    assert calls["setup"] == 1
    assert calls["shutdown"] == 1
```

---

## Implementation Order

| Step | Files | Effort | Dependencies |
| ---- | ----- | ------ | ------------ |
| 1 | `pyproject.toml` | 5 min | None |
| 2 | `config.py` | 15 min | None |
| 3 | `telemetry.py` (new) | 1-2h | Steps 1, 2 |
| 4 | `cli.py` | 15 min | Step 3 |
| 5 | `server.py` (print -> logging) | 10 min | None (parallel) |
| 6 | `search.py` (spans + metrics) | 30 min | Step 3 |
| 7 | `coordination.py` (spans + metrics) | 30 min | Step 3 |
| 8 | `knowledge.py` (spans + metrics) | 20 min | Step 3 |
| 9 | `server.py` (tool spans) | 45 min | Step 3 |
| 10 | `test_telemetry.py` (new) | 1h | Step 3 |

Total: ~4-5 hours

---

## Key Points

- **docker logs keeps working** -- OTEL is additive, stdout is unchanged
- **OTEL is opt-in** -- disabled by default, minimal overhead for contributors
- **Console fallback** -- useful for dev without the full OTEL stack
- **Graceful degradation** -- lithos runs fine if OTEL packages are not installed
- **One collector, many services** -- future services just point at `otel-collector:4318`
