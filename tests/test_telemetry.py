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
