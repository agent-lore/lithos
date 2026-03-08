"""Tests for OpenTelemetry instrumentation."""

import asyncio
import json
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
