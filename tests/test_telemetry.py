"""Tests for OpenTelemetry instrumentation."""

import asyncio
import json
import logging
from typing import Any

import pytest

from lithos.server import LithosServer


def _extract_id(mcp_result: Any) -> str:
    """Extract doc ID from an MCP tool result."""
    # Handle tuple form (some FastMCP versions)
    if isinstance(mcp_result, tuple):
        payload = mcp_result[1]
        if isinstance(payload, dict):
            return payload["id"]

    # Handle content-list form
    content = getattr(mcp_result, "content", []) if hasattr(mcp_result, "content") else mcp_result
    if isinstance(content, list) and content:
        text = getattr(content[0], "text", None)
        if isinstance(text, str):
            return json.loads(text)["id"]

    raise AssertionError(f"Unable to extract ID from MCP result: {mcp_result!r}")


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
        # Reset cached instruments so they get recreated as no-ops
        lithos_metrics._knowledge_ops = None
        lithos_metrics._search_duration = None
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
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

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
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

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


@pytest.mark.integration
@pytest.mark.skipif(
    not _has_otel_packages(),
    reason="opentelemetry-sdk not installed",
)
class TestTelemetryIntegration:
    """Integration tests: verify spans flow through tool handler + service layers."""

    @pytest.fixture
    async def otel_server(self, test_config):
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        from lithos.telemetry import _reset_for_testing, setup_telemetry, shutdown_telemetry

        _reset_for_testing()
        test_config.telemetry.enabled = True
        exporter = InMemorySpanExporter()
        setup_telemetry(test_config, _test_span_exporter=exporter)

        srv = LithosServer(test_config)
        await srv.initialize()
        yield srv, exporter

        srv.stop_file_watcher()
        shutdown_telemetry()
        _reset_for_testing()

    async def test_write_produces_tool_and_service_spans(self, otel_server):
        server, exporter = otel_server

        result = await server.mcp._call_tool_mcp(
            "lithos_write",
            {
                "title": "Telemetry Test Doc",
                "content": "Testing span propagation through the stack.",
                "agent": "test-agent",
                "tags": ["telemetry"],
            },
        )

        # Extract doc_id from result to verify the write succeeded
        content = getattr(result, "content", []) if hasattr(result, "content") else result
        if isinstance(content, list) and content:
            text = getattr(content[0], "text", None)
            if isinstance(text, str):
                payload = json.loads(text)
                assert "id" in payload

        spans = exporter.get_finished_spans()
        span_names = [s.name for s in spans]

        # Tool handler span
        assert "lithos.tool.write" in span_names

        # Service layer span from @traced decorator on knowledge.create
        assert "lithos.knowledge.create" in span_names

        # Verify tool span has the lithos.tool attribute
        tool_span = next(s for s in spans if s.name == "lithos.tool.write")
        tool_attrs = dict(tool_span.attributes)
        assert tool_attrs.get("lithos.tool") == "lithos_write"

    async def test_write_with_provenance_records_span_attributes(self, otel_server):
        """Write with derived_from_ids records provenance counts on span."""
        server, exporter = otel_server

        # Create a source doc first
        source_result = await server.mcp._call_tool_mcp(
            "lithos_write",
            {
                "title": "OTEL Source",
                "content": "Source doc.",
                "agent": "test-agent",
            },
        )
        source_id = _extract_id(source_result)

        exporter.clear()

        # Create a derived doc referencing the source
        await server.mcp._call_tool_mcp(
            "lithos_write",
            {
                "title": "OTEL Derived",
                "content": "Derived doc.",
                "agent": "test-agent",
                "derived_from_ids": [source_id],
            },
        )

        spans = exporter.get_finished_spans()
        tool_span = next(s for s in spans if s.name == "lithos.tool.write")
        tool_attrs = dict(tool_span.attributes)
        assert tool_attrs.get("lithos.provenance.source_count") == 1

    async def test_provenance_tool_emits_span(self, otel_server):
        """lithos_provenance emits a span with direction and depth."""
        server, exporter = otel_server

        # Create a doc
        result = await server.mcp._call_tool_mcp(
            "lithos_write",
            {
                "title": "OTEL Prov Query",
                "content": "Test.",
                "agent": "test-agent",
            },
        )
        doc_id = _extract_id(result)

        exporter.clear()

        # Call lithos_provenance
        await server.mcp._call_tool_mcp(
            "lithos_provenance",
            {"id": doc_id, "direction": "both", "depth": 1},
        )

        spans = exporter.get_finished_spans()
        span_names = [s.name for s in spans]
        assert "lithos.tool.provenance" in span_names

        prov_span = next(s for s in spans if s.name == "lithos.tool.provenance")
        prov_attrs = dict(prov_span.attributes)
        assert prov_attrs.get("lithos.tool") == "lithos_provenance"
        assert prov_attrs.get("lithos.direction") == "both"
        assert prov_attrs.get("lithos.depth") == 1
        assert "lithos.sources_count" in prov_attrs
        assert "lithos.derived_count" in prov_attrs


class TestWriteDurationHistogram:
    """Tests for lithos.knowledge.write_duration_ms histogram (issue #89)."""

    def test_timed_write_decorator_on_sync_function(self):
        """@timed_write wraps sync functions and does not raise."""
        from lithos.telemetry import timed_write

        @timed_write("create")
        def my_fn() -> str:
            return "ok"

        assert my_fn() == "ok"

    @pytest.mark.asyncio
    async def test_timed_write_decorator_on_async_function(self):
        """@timed_write wraps async functions and does not raise."""
        from lithos.telemetry import timed_write

        @timed_write("update")
        async def my_async_fn() -> int:
            return 42

        assert await my_async_fn() == 42

    @pytest.mark.asyncio
    async def test_timed_write_marks_failure_on_exception(self):
        """@timed_write re-raises exceptions and marks success=False."""

        from lithos.telemetry import timed_write

        calls: list[dict] = []

        @timed_write("create")
        async def failing_fn() -> None:
            raise RuntimeError("boom")

        # Patch lithos_metrics to capture the recording
        from lithos import telemetry as tel

        original_histogram = tel.lithos_metrics._knowledge_write_duration

        class _FakeHistogram:
            def record(self, value: float, attrs: dict) -> None:
                calls.append({"value": value, "attrs": attrs})

        tel.lithos_metrics._knowledge_write_duration = _FakeHistogram()
        try:
            with pytest.raises(RuntimeError, match="boom"):
                await failing_fn()
        finally:
            tel.lithos_metrics._knowledge_write_duration = original_histogram

        assert len(calls) == 1
        assert calls[0]["attrs"]["success"] is False
        assert calls[0]["attrs"]["op"] == "create"
        assert calls[0]["value"] >= 0

    def test_knowledge_create_has_timed_write(self):
        """KnowledgeManager.create carries the @timed_write decorator."""
        from lithos.knowledge import KnowledgeManager

        # @timed_write uses functools.wraps, so __wrapped__ is set
        assert hasattr(KnowledgeManager.create, "__wrapped__"), (
            "KnowledgeManager.create is missing @timed_write"
        )

    def test_knowledge_update_has_timed_write(self):
        """KnowledgeManager.update carries the @timed_write decorator."""
        from lithos.knowledge import KnowledgeManager

        assert hasattr(KnowledgeManager.update, "__wrapped__"), (
            "KnowledgeManager.update is missing @timed_write"
        )

    def test_write_duration_histogram_registered(self):
        """lithos_metrics.knowledge_write_duration returns a histogram-like object."""
        from lithos.telemetry import lithos_metrics

        hist = lithos_metrics.knowledge_write_duration
        assert hasattr(hist, "record"), "knowledge_write_duration must have a .record() method"


class TestTracingCoverage:
    """Regression tests that verify @traced decorators are present on key methods."""

    def test_knowledge_list_all_is_traced(self):
        """list_all must carry a @traced decorator."""
        from lithos.knowledge import KnowledgeManager

        # After @traced wraps the function, __wrapped__ is set by functools.wraps
        assert hasattr(KnowledgeManager.list_all, "__wrapped__"), (
            "KnowledgeManager.list_all is missing @traced"
        )

    def test_graph_load_cache_is_traced(self):
        from lithos.graph import KnowledgeGraph

        assert hasattr(KnowledgeGraph.load_cache, "__wrapped__"), (
            "KnowledgeGraph.load_cache is missing @traced"
        )

    def test_graph_save_cache_is_traced(self):
        from lithos.graph import KnowledgeGraph

        assert hasattr(KnowledgeGraph.save_cache, "__wrapped__"), (
            "KnowledgeGraph.save_cache is missing @traced"
        )

    def test_graph_add_document_is_traced(self):
        from lithos.graph import KnowledgeGraph

        assert hasattr(KnowledgeGraph.add_document, "__wrapped__"), (
            "KnowledgeGraph.add_document is missing @traced"
        )

    def test_graph_get_links_is_traced(self):
        from lithos.graph import KnowledgeGraph

        assert hasattr(KnowledgeGraph.get_links, "__wrapped__"), (
            "KnowledgeGraph.get_links is missing @traced"
        )

    def test_coordination_initialize_is_traced(self):
        from lithos.coordination import CoordinationService

        assert hasattr(CoordinationService.initialize, "__wrapped__"), (
            "CoordinationService.initialize is missing @traced"
        )

    def test_coordination_ensure_agent_known_is_traced(self):
        from lithos.coordination import CoordinationService

        assert hasattr(CoordinationService.ensure_agent_known, "__wrapped__"), (
            "CoordinationService.ensure_agent_known is missing @traced"
        )

    def test_coordination_get_task_is_traced(self):
        from lithos.coordination import CoordinationService

        assert hasattr(CoordinationService.get_task, "__wrapped__"), (
            "CoordinationService.get_task is missing @traced"
        )

    def test_coordination_get_task_status_is_traced(self):
        from lithos.coordination import CoordinationService

        assert hasattr(CoordinationService.get_task_status, "__wrapped__"), (
            "CoordinationService.get_task_status is missing @traced"
        )

    def test_coordination_post_finding_is_traced(self):
        from lithos.coordination import CoordinationService

        assert hasattr(CoordinationService.post_finding, "__wrapped__"), (
            "CoordinationService.post_finding is missing @traced"
        )

    def test_coordination_list_findings_is_traced(self):
        from lithos.coordination import CoordinationService

        assert hasattr(CoordinationService.list_findings, "__wrapped__"), (
            "CoordinationService.list_findings is missing @traced"
        )


class TestTraceLogCorrelation:
    """Tests for trace_id/span_id injection into log records (issue #90)."""

    def test_filter_adds_fields_when_inactive(self):
        """_TraceContextFilter adds otelTraceID=zeros when OTEL is inactive."""
        from lithos.telemetry import _TraceContextFilter

        flt = _TraceContextFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="hello",
            args=(),
            exc_info=None,
        )
        result = flt.filter(record)
        assert result is True
        assert record.otelTraceID == "0" * 32
        assert record.otelSpanID == "0" * 16
        assert record.otelTraceSampled is False
        assert record.otelServiceName == "lithos"

    def test_inject_trace_context_is_idempotent(self):
        """_inject_trace_context_into_logs() does not install the filter twice."""
        from lithos.telemetry import _inject_trace_context_into_logs, _reset_for_testing

        _reset_for_testing()
        _inject_trace_context_into_logs()
        _inject_trace_context_into_logs()  # second call must be a no-op

        root = logging.getLogger()
        count = sum(1 for f in root.filters if f.__class__.__name__ == "_TraceContextFilter")
        assert count == 1

        # Cleanup
        _reset_for_testing()

    def test_filter_removed_on_reset(self):
        """_reset_for_testing() removes the filter from the root logger."""
        from lithos.telemetry import _inject_trace_context_into_logs, _reset_for_testing

        _reset_for_testing()
        _inject_trace_context_into_logs()

        root = logging.getLogger()
        before = sum(1 for f in root.filters if f.__class__.__name__ == "_TraceContextFilter")
        assert before == 1

        _reset_for_testing()
        after = sum(1 for f in root.filters if f.__class__.__name__ == "_TraceContextFilter")
        assert after == 0

    def test_trace_id_injected_within_active_span(self, test_config):
        """When inside a span, otelTraceID is a non-zero hex string."""
        pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        from lithos.telemetry import (
            _reset_for_testing,
            _TraceContextFilter,
            get_tracer,
            setup_telemetry,
            shutdown_telemetry,
        )

        _reset_for_testing()
        test_config.telemetry.enabled = True
        setup_telemetry(test_config, _test_span_exporter=InMemorySpanExporter())

        flt = _TraceContextFilter()
        tracer = get_tracer()

        try:
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg="in-span",
                args=(),
                exc_info=None,
            )
            with tracer.start_as_current_span("test-span"):
                flt.filter(record)

            # Inside a span the trace ID must be non-zero
            assert record.otelTraceID != "0" * 32
            assert len(record.otelTraceID) == 32
            assert record.otelSpanID != "0" * 16
            assert len(record.otelSpanID) == 16
        finally:
            shutdown_telemetry()
            _reset_for_testing()

    def test_setup_telemetry_installs_filter(self, test_config):
        """setup_telemetry() always installs _TraceContextFilter on the root logger."""
        pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        from lithos.telemetry import _reset_for_testing, setup_telemetry

        _reset_for_testing()
        test_config.telemetry.enabled = True
        try:
            exporter = InMemorySpanExporter()
            setup_telemetry(test_config, _test_span_exporter=exporter)

            root = logging.getLogger()
            assert any(f.__class__.__name__ == "_TraceContextFilter" for f in root.filters), (
                "setup_telemetry should install _TraceContextFilter"
            )
        finally:
            _reset_for_testing()

    def test_globally_installed_filter_injects_trace_context(self, test_config):
        """End-to-end: globally-installed filter (via setup_telemetry) injects real trace IDs."""
        pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        import lithos.telemetry as tel
        from lithos.telemetry import (
            _reset_for_testing,
            get_tracer,
            setup_telemetry,
            shutdown_telemetry,
        )

        _reset_for_testing()
        test_config.telemetry.enabled = True
        test_config.telemetry.service_name = "my-test-service"
        setup_telemetry(test_config, _test_span_exporter=InMemorySpanExporter())

        # Retrieve the globally-installed filter (not a fresh instance)
        installed_filter = tel._trace_context_filter
        assert installed_filter is not None, "Filter must be installed after setup_telemetry()"

        tracer = get_tracer()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="e2e-test",
            args=(),
            exc_info=None,
        )
        try:
            with tracer.start_as_current_span("e2e-span"):
                installed_filter.filter(record)

            # Inside the span, real trace context must be injected
            assert record.otelTraceID != "0" * 32, "Expected non-zero trace ID within span"
            assert len(record.otelTraceID) == 32
            assert record.otelSpanID != "0" * 16, "Expected non-zero span ID within span"
            assert len(record.otelSpanID) == 16
            assert record.otelTraceSampled is True
            assert record.otelServiceName == "my-test-service"
        finally:
            shutdown_telemetry()
            _reset_for_testing()

    def test_filter_uses_configured_service_name(self, test_config):
        """_TraceContextFilter uses the service_name passed at construction, not a hardcoded value."""
        pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        import lithos.telemetry as tel
        from lithos.telemetry import _reset_for_testing, setup_telemetry, shutdown_telemetry

        _reset_for_testing()
        test_config.telemetry.enabled = True
        test_config.telemetry.service_name = "custom-svc"
        try:
            setup_telemetry(test_config, _test_span_exporter=InMemorySpanExporter())
            installed_filter = tel._trace_context_filter
            assert installed_filter is not None

            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg="svc-name-test",
                args=(),
                exc_info=None,
            )
            installed_filter.filter(record)
            assert record.otelServiceName == "custom-svc", (
                f"Expected 'custom-svc', got '{record.otelServiceName}'"
            )
        finally:
            shutdown_telemetry()
            _reset_for_testing()


class TestToolMetrics:
    """Tests for the @tool_metrics decorator and per-tool counters (issue #101)."""

    def test_tool_metrics_passthrough_sync(self):
        """@tool_metrics wraps sync functions and returns the correct value."""
        from lithos.telemetry import tool_metrics

        @tool_metrics()
        def my_fn(x: int) -> int:
            return x * 2

        assert my_fn(5) == 10

    @pytest.mark.asyncio
    async def test_tool_metrics_passthrough_async(self):
        """@tool_metrics wraps async functions and returns the correct value."""
        from lithos.telemetry import tool_metrics

        @tool_metrics()
        async def my_async_fn(x: int) -> int:
            return x * 3

        assert await my_async_fn(7) == 21

    def test_tool_metrics_preserves_function_name(self):
        """@tool_metrics preserves __name__ via functools.wraps."""
        from lithos.telemetry import tool_metrics

        @tool_metrics()
        async def lithos_write() -> None:
            pass

        assert lithos_write.__name__ == "lithos_write"

    def test_tool_metrics_explicit_name_overrides(self):
        """Explicit tool_name argument is used instead of func.__name__."""
        from lithos import telemetry as tel
        from lithos.telemetry import tool_metrics

        calls: list[dict] = []

        class _FakeCounter:
            def add(self, amount: int, attributes: dict | None = None) -> None:
                calls.append({"amount": amount, "attributes": attributes})

        tel.lithos_metrics._tool_calls = _FakeCounter()
        try:

            @tool_metrics("custom_name")
            def my_fn() -> str:
                return "ok"

            my_fn()
            assert any(c["attributes"] == {"tool_name": "custom_name"} for c in calls)
        finally:
            tel.lithos_metrics._tool_calls = None

    def test_tool_calls_incremented_on_invocation(self):
        """lithos_metrics.tool_calls.add is called once per tool invocation."""
        from lithos import telemetry as tel
        from lithos.telemetry import tool_metrics

        calls: list[dict] = []

        class _FakeCounter:
            def add(self, amount: int, attributes: dict | None = None) -> None:
                calls.append({"amount": amount, "attributes": attributes})

        tel.lithos_metrics._tool_calls = _FakeCounter()
        tel.lithos_metrics._tool_errors = _FakeCounter()

        try:

            @tool_metrics()
            def lithos_search() -> str:
                return "results"

            lithos_search()
            lithos_search()

            tool_call_entries = [
                c for c in calls if c["attributes"] == {"tool_name": "lithos_search"}
            ]
            assert len(tool_call_entries) == 2
        finally:
            tel.lithos_metrics._tool_calls = None
            tel.lithos_metrics._tool_errors = None

    @pytest.mark.asyncio
    async def test_tool_errors_incremented_on_exception(self):
        """lithos_metrics.tool_errors.add is called when the handler raises."""
        from lithos import telemetry as tel
        from lithos.telemetry import tool_metrics

        error_calls: list[dict] = []
        call_calls: list[dict] = []

        class _FakeCallCounter:
            def add(self, amount: int, attributes: dict | None = None) -> None:
                call_calls.append({"amount": amount, "attributes": attributes})

        class _FakeErrorCounter:
            def add(self, amount: int, attributes: dict | None = None) -> None:
                error_calls.append({"amount": amount, "attributes": attributes})

        tel.lithos_metrics._tool_calls = _FakeCallCounter()
        tel.lithos_metrics._tool_errors = _FakeErrorCounter()

        try:

            @tool_metrics()
            async def lithos_write() -> None:
                raise ValueError("bad input")

            with pytest.raises(ValueError):
                await lithos_write()

            assert len(call_calls) == 1
            assert call_calls[0]["attributes"] == {"tool_name": "lithos_write"}

            assert len(error_calls) == 1
            assert error_calls[0]["attributes"] == {
                "tool_name": "lithos_write",
                "error_type": "ValueError",
            }
        finally:
            tel.lithos_metrics._tool_calls = None
            tel.lithos_metrics._tool_errors = None

    @pytest.mark.asyncio
    async def test_tool_errors_not_incremented_on_success(self):
        """tool_errors counter must NOT be incremented for successful calls."""
        from lithos import telemetry as tel
        from lithos.telemetry import tool_metrics

        error_calls: list[dict] = []

        class _FakeCallCounter:
            def add(self, amount: int, attributes: dict | None = None) -> None:
                pass

        class _FakeErrorCounter:
            def add(self, amount: int, attributes: dict | None = None) -> None:
                error_calls.append({"amount": amount, "attributes": attributes})

        tel.lithos_metrics._tool_calls = _FakeCallCounter()
        tel.lithos_metrics._tool_errors = _FakeErrorCounter()

        try:

            @tool_metrics()
            async def lithos_read() -> str:
                return "doc content"

            await lithos_read()
            assert error_calls == []
        finally:
            tel.lithos_metrics._tool_calls = None
            tel.lithos_metrics._tool_errors = None

    def test_tool_metrics_noop_when_telemetry_disabled(self):
        """tool_metrics is a transparent no-op when OTEL is not configured."""
        from lithos.telemetry import _reset_for_testing, tool_metrics

        _reset_for_testing()

        @tool_metrics()
        def lithos_stats() -> int:
            return 42

        assert lithos_stats() == 42

    def test_tool_calls_counter_has_add_method(self):
        """lithos_metrics.tool_calls exposes .add()."""
        from lithos.telemetry import lithos_metrics

        counter = lithos_metrics.tool_calls
        assert hasattr(counter, "add"), "tool_calls must expose .add()"

    def test_tool_errors_counter_has_add_method(self):
        """lithos_metrics.tool_errors exposes .add()."""
        from lithos.telemetry import lithos_metrics

        counter = lithos_metrics.tool_errors
        assert hasattr(counter, "add"), "tool_errors must expose .add()"

    def test_all_server_tools_have_tool_metrics_decorator(self):
        """Every @self.mcp.tool() in LithosServer._register_tools also has @tool_metrics().

        Counts occurrences of each decorator in the source so the assertion
        automatically catches new tools added without the metrics decorator.
        """
        import inspect

        from lithos.server import LithosServer

        source = inspect.getsource(LithosServer._register_tools)
        mcp_tool_count = source.count("@self.mcp.tool()")
        metered_count = source.count("@tool_metrics()")
        assert metered_count == mcp_tool_count, (
            f"Missing @tool_metrics() on {mcp_tool_count - metered_count} tool(s): "
            f"found {metered_count} @tool_metrics() decorator(s) but "
            f"{mcp_tool_count} @self.mcp.tool() decorator(s)"
        )

    def test_tool_metrics_preserves_signature_for_mcp_introspection(self):
        """@tool_metrics() does not break the parameter schema seen by the MCP SDK.

        fastmcp calls inspect.signature(fn) to build the JSON schema for each
        registered tool.  Because tool_metrics uses functools.wraps, inspect
        follows __wrapped__ and recovers the original signature automatically.
        """
        import inspect

        from lithos.telemetry import tool_metrics

        async def lithos_fake(title: str, content: str, agent: str = "anon") -> dict:
            """Fake tool with a realistic signature."""
            return {}

        wrapped = tool_metrics()(lithos_fake)

        # __wrapped__ must point back to the original
        assert wrapped.__wrapped__ is lithos_fake, (
            "@tool_metrics must set __wrapped__ (via functools.wraps)"
        )

        # inspect.signature must follow __wrapped__ and see the original params
        orig_sig = inspect.signature(lithos_fake)
        wrapped_sig = inspect.signature(wrapped)
        assert list(orig_sig.parameters) == list(wrapped_sig.parameters), (
            "inspect.signature on the @tool_metrics wrapper returned different "
            f"parameters than the original: {list(wrapped_sig.parameters)} != "
            f"{list(orig_sig.parameters)}"
        )


class TestResourceGauges:
    """Tests for register_resource_gauges (issue #87)."""

    def test_register_resource_gauges_no_raise_without_otel(self):
        """register_resource_gauges must not raise when OTEL is inactive."""
        from lithos.telemetry import register_resource_gauges

        # Should be a clean no-op when _initialized is False
        register_resource_gauges(
            get_document_count=lambda: 5,
            get_stale_document_count=lambda: 1,
            get_tantivy_document_count=lambda: 5,
            get_chroma_chunk_count=lambda: 20,
            get_graph_node_count=lambda: 5,
            get_graph_edge_count=lambda: 4,
            get_agent_count=lambda: 2,
        )

    def test_knowledge_manager_document_count_property(self, tmp_path):
        """KnowledgeManager.document_count returns len(_meta_cache)."""
        from lithos.config import LithosConfig, StorageConfig
        from lithos.knowledge import KnowledgeManager

        config = LithosConfig(storage=StorageConfig(data_dir=str(tmp_path)))
        km = KnowledgeManager(config)
        assert km.document_count == 0

    def test_knowledge_manager_stale_document_count_zero_when_no_expiry(self, tmp_path):
        """stale_document_count is 0 when no documents have expires_at set."""
        from lithos.config import LithosConfig, StorageConfig
        from lithos.knowledge import KnowledgeManager

        config = LithosConfig(storage=StorageConfig(data_dir=str(tmp_path)))
        km = KnowledgeManager(config)
        assert km.stale_document_count == 0

    @pytest.mark.asyncio
    async def test_stale_document_count_increments_for_expired_doc(self, tmp_path):
        """stale_document_count counts docs whose expires_at is in the past."""
        from datetime import timedelta

        from lithos.config import LithosConfig, StorageConfig
        from lithos.knowledge import KnowledgeManager

        config = LithosConfig(storage=StorageConfig(data_dir=str(tmp_path)))
        km = KnowledgeManager(config)

        # Create a doc that expires in the past
        from datetime import datetime, timezone

        past = datetime.now(timezone.utc) - timedelta(hours=1)
        await km.create(
            title="Stale Doc",
            content="This doc is stale.",
            agent="test",
            expires_at=past,
        )
        assert km.stale_document_count == 1

    @pytest.mark.asyncio
    async def test_non_stale_doc_not_counted(self, tmp_path):
        """stale_document_count does not count docs that haven't expired yet."""
        from datetime import timedelta

        from lithos.config import LithosConfig, StorageConfig
        from lithos.knowledge import KnowledgeManager

        config = LithosConfig(storage=StorageConfig(data_dir=str(tmp_path)))
        km = KnowledgeManager(config)

        from datetime import datetime, timezone

        future = datetime.now(timezone.utc) + timedelta(hours=1)
        await km.create(
            title="Fresh Doc",
            content="Not expired yet.",
            agent="test",
            expires_at=future,
        )
        assert km.stale_document_count == 0


class TestStartupDurationMetric:
    """Tests for lithos.startup_duration_ms histogram (issue #99)."""

    def test_startup_duration_noop_does_not_raise(self):
        """startup_duration.record() works without OTEL active (no-op path)."""
        from lithos.telemetry import _reset_for_testing, lithos_metrics

        _reset_for_testing()
        lithos_metrics._startup_duration = None  # reset cached instrument
        # Should not raise on the no-op path
        lithos_metrics.startup_duration.record(42.0)

    async def test_initialize_records_startup_duration(self, test_config) -> None:
        """initialize() must call startup_duration.record() with a positive elapsed time."""
        from unittest.mock import MagicMock

        import lithos.telemetry as tel_module
        from lithos.server import LithosServer

        mock_hist = MagicMock()
        orig = tel_module.lithos_metrics._startup_duration
        tel_module.lithos_metrics._startup_duration = mock_hist
        try:
            srv = LithosServer(test_config)
            await srv.initialize()
            srv.stop_file_watcher()
        finally:
            tel_module.lithos_metrics._startup_duration = orig

        mock_hist.record.assert_called_once()
        recorded_value = mock_hist.record.call_args[0][0]
        assert recorded_value > 0, f"Expected a positive startup duration, got {recorded_value!r}"


class TestFileWatcherEventsCounter:
    """Tests for lithos.file_watcher.events_total counter (issue #99)."""

    def test_file_watcher_events_noop_does_not_raise(self):
        """file_watcher_events.add() works without OTEL active (no-op path)."""
        from lithos.telemetry import _reset_for_testing, lithos_metrics

        _reset_for_testing()
        lithos_metrics._file_watcher_events = None  # reset cached instrument
        # Should not raise on the no-op path
        lithos_metrics.file_watcher_events.add(1, {"event_type": "created"})
        lithos_metrics.file_watcher_events.add(1, {"event_type": "updated"})
        lithos_metrics.file_watcher_events.add(1, {"event_type": "deleted"})

    async def test_handle_file_change_increments_deleted_counter(
        self, server: "LithosServer"
    ) -> None:
        """handle_file_change(deleted=True) increments the counter with event_type=deleted."""
        from unittest.mock import patch

        from lithos.telemetry import lithos_metrics

        lithos_metrics._file_watcher_events = None
        counter = lithos_metrics.file_watcher_events  # prime no-op instance

        doc = (
            await server.knowledge.create(
                title="Counter Delete Test",
                content="To be deleted.",
                agent="test-agent",
            )
        ).document
        server.search.index_document(doc)
        server.graph.add_document(doc)

        file_path = server.config.storage.knowledge_path / doc.path
        file_path.unlink()

        with patch.object(counter, "add") as mock_add:
            await server.handle_file_change(file_path, deleted=True)
            mock_add.assert_called_once_with(1, {"event_type": "deleted"})

    async def test_handle_file_change_increments_updated_counter(
        self, server: "LithosServer"
    ) -> None:
        """handle_file_change(deleted=False) on existing file increments updated counter."""
        from unittest.mock import patch

        from lithos.telemetry import lithos_metrics

        lithos_metrics._file_watcher_events = None
        counter = lithos_metrics.file_watcher_events  # prime no-op instance

        doc = (
            await server.knowledge.create(
                title="Counter Update Test",
                content="Existing doc.",
                agent="test-agent",
            )
        ).document
        server.search.index_document(doc)
        server.graph.add_document(doc)

        file_path = server.config.storage.knowledge_path / doc.path

        with patch.object(counter, "add") as mock_add:
            await server.handle_file_change(file_path, deleted=False)
            mock_add.assert_called_once_with(1, {"event_type": "updated"})

    async def test_handle_file_change_increments_created_counter(
        self, server: "LithosServer"
    ) -> None:
        """handle_file_change(deleted=False) on a NEW file increments the counter with event_type=created."""
        from unittest.mock import patch

        from lithos.telemetry import lithos_metrics

        lithos_metrics._file_watcher_events = None
        counter = lithos_metrics.file_watcher_events  # prime no-op instance

        # Write a brand-new .md file directly — no existing document in the store
        new_file = server.config.storage.knowledge_path / "new-test-file.md"
        new_file.write_text("---\ntitle: Brand New Doc\nagent: test-agent\n---\nHello.\n")

        with patch.object(counter, "add") as mock_add:
            await server.handle_file_change(new_file, deleted=False)
            mock_add.assert_called_once_with(1, {"event_type": "created"})


class TestSSEMetrics:
    """Tests for SSE client metrics (issue #97)."""

    def test_register_sse_active_clients_observer_no_raise_without_otel(self):
        """register_sse_active_clients_observer must not raise when OTEL is inactive."""
        from lithos.telemetry import register_sse_active_clients_observer

        # Should be a clean no-op when _initialized is False
        register_sse_active_clients_observer(lambda: 3)

    def test_sse_events_delivered_counter_no_raise_without_otel(self):
        """lithos_metrics.sse_events_delivered.add() must not raise when OTEL is inactive."""
        from lithos.telemetry import lithos_metrics

        # Instrument is created lazily; calling add() on the no-op meter must not raise
        lithos_metrics.sse_events_delivered.add(1)

    def test_sse_gauge_callback_reads_client_count(self):
        """Observable gauge callback must return [Observation(count)] for the current value."""
        from opentelemetry.metrics import Observation

        from lithos.telemetry import register_sse_active_clients_observer

        captured_callback: list = []

        # Capture the callback by temporarily monkey-patching create_observable_gauge
        from unittest.mock import MagicMock, patch

        import lithos.telemetry as tel_module

        mock_meter = MagicMock()

        def _capture_gauge(name, *, callbacks, **kwargs):
            captured_callback.extend(callbacks)

        mock_meter.create_observable_gauge.side_effect = _capture_gauge

        with patch.object(tel_module, "get_meter", return_value=mock_meter):
            # Reset the sentinel so the gauge is re-registered in this isolated call
            orig = tel_module._sse_active_clients_gauge_registered
            tel_module._sse_active_clients_gauge_registered = False
            try:
                count_holder = [7]
                register_sse_active_clients_observer(lambda: count_holder[0])
            finally:
                tel_module._sse_active_clients_gauge_registered = orig

        assert len(captured_callback) == 1, "Expected exactly one callback to be registered"
        result = captured_callback[0](None)
        assert result == [Observation(7)], f"Expected [Observation(7)], got {result!r}"

        # Verify callback reflects updated count
        count_holder[0] = 42
        assert captured_callback[0](None) == [Observation(42)]

    def test_sse_events_delivered_counter_increments(self):
        """sse_events_delivered.add() must be called once per event yielded."""
        from unittest.mock import MagicMock

        import lithos.telemetry as tel_module

        mock_counter = MagicMock()
        # sse_events_delivered is a lazy property backed by _sse_events_delivered
        orig = tel_module.lithos_metrics._sse_events_delivered
        tel_module.lithos_metrics._sse_events_delivered = mock_counter
        try:
            for _ in range(3):
                tel_module.lithos_metrics.sse_events_delivered.add(1)
        finally:
            tel_module.lithos_metrics._sse_events_delivered = orig

        assert mock_counter.add.call_count == 3

    async def test_sse_client_count_decrements_on_disconnect(self):
        """_sse_client_count must decrement when the SSE async generator is closed early."""
        from unittest.mock import MagicMock

        from starlette.requests import Request

        import lithos.telemetry as tel_module
        from lithos.config import LithosConfig
        from lithos.events import EventBus, LithosEvent
        from lithos.server import LithosServer

        # Build a minimal server skeleton — avoid full initialise()
        server = LithosServer.__new__(LithosServer)
        server._sse_client_count = 0

        cfg = LithosConfig()
        server._config = cfg

        # No auth needed
        server.mcp = MagicMock()
        server.mcp.auth = None

        bus = EventBus()
        server.event_bus = bus

        # Enqueue one event so the generator has something to yield before we close it
        evt = LithosEvent(type="test", agent="forge")
        await bus.emit(evt)

        # Build a minimal Starlette Request from a raw scope dict (no real socket)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/events",
            "query_string": b"",
            "headers": [],
        }
        request = Request(scope)

        # Patch the lazy counter backing field so real OTEL isn't needed
        mock_counter = MagicMock()
        orig = tel_module.lithos_metrics._sse_events_delivered
        tel_module.lithos_metrics._sse_events_delivered = mock_counter
        try:
            response = await server._sse_endpoint(request)

            # At this point the count must have been incremented
            assert server._sse_client_count == 1, (
                f"Expected _sse_client_count=1 before streaming, got {server._sse_client_count}"
            )

            body_iter = response.body_iterator

            # Consume one chunk then explicitly close the async generator to
            # trigger GeneratorExit and run the finally block
            import contextlib

            async with contextlib.AsyncExitStack():
                with contextlib.suppress(StopAsyncIteration):
                    await body_iter.__anext__()

            # Explicitly close the generator (triggers GeneratorExit → finally block)
            if hasattr(body_iter, "aclose"):
                await body_iter.aclose()

            # After closing, the finally block must have run and decremented
            assert server._sse_client_count == 0, (
                f"Expected _sse_client_count=0 after disconnect, got {server._sse_client_count}"
            )
        finally:
            tel_module.lithos_metrics._sse_events_delivered = orig

    def test_register_sse_active_clients_observer_idempotent(self):
        """Calling register_sse_active_clients_observer twice must only register one gauge."""
        from unittest.mock import MagicMock, patch

        import lithos.telemetry as tel_module

        mock_meter = MagicMock()

        with patch.object(tel_module, "get_meter", return_value=mock_meter):
            orig = tel_module._sse_active_clients_gauge_registered
            tel_module._sse_active_clients_gauge_registered = False
            try:
                register_fn = tel_module.register_sse_active_clients_observer
                register_fn(lambda: 0)
                register_fn(lambda: 1)
            finally:
                tel_module._sse_active_clients_gauge_registered = orig

        # create_observable_gauge must be called exactly once
        assert mock_meter.create_observable_gauge.call_count == 1
