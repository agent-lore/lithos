"""Unit tests for the tool_span tracing/error-mapping seam."""

import inspect

import pytest

from lithos.errors import CoordinationError
from lithos.telemetry import tool_metrics
from lithos.tools._seam import tool_span


class TestToolSpan:
    async def test_returns_handler_result(self):
        @tool_span()
        async def lithos_demo(a: int) -> dict:
            """Demo."""
            return {"a": a}

        assert await lithos_demo(3) == {"a": 3}

    async def test_maps_coordination_error_to_envelope(self):
        @tool_span(map_coordination_error=True)
        async def lithos_demo() -> dict:
            """Demo."""
            raise CoordinationError("cycle", "edge would create a cycle")

        assert await lithos_demo() == {
            "status": "error",
            "code": "cycle",
            "message": "edge would create a cycle",
        }

    async def test_without_mapping_coordination_error_propagates(self):
        @tool_span()
        async def lithos_demo() -> dict:
            """Demo."""
            raise CoordinationError("cycle", "boom")

        with pytest.raises(CoordinationError):
            await lithos_demo()

    async def test_other_exceptions_propagate_even_with_mapping(self):
        @tool_span(map_coordination_error=True)
        async def lithos_demo() -> dict:
            """Demo."""
            raise RuntimeError("not a coordination error")

        with pytest.raises(RuntimeError):
            await lithos_demo()

    def test_wraps_preserves_signature_and_docstring(self):
        """fastmcp derives the tool schema from inspect.signature and the
        description from __doc__ — both must survive the full decorator stack."""

        async def lithos_demo(title: str, content: str, agent: str = "anon") -> dict:
            """Docstring is the MCP tool description."""
            return {}

        stacked = tool_metrics()(tool_span(map_coordination_error=True)(lithos_demo))

        assert stacked.__name__ == "lithos_demo"
        assert stacked.__doc__ == "Docstring is the MCP tool description."
        assert list(inspect.signature(stacked).parameters) == ["title", "content", "agent"]

    async def test_mapped_coordination_error_not_counted_as_tool_error(self, monkeypatch):
        """The stack order contract: tool_span sits below tool_metrics, so a
        mapped CoordinationError returns normally and is NOT a tool error,
        while a raised exception still is."""
        from lithos import telemetry

        calls: list[tuple[str, dict]] = []

        class _Recorder:
            def add(self, value, attributes=None):
                calls.append(("add", attributes or {}))

        monkeypatch.setattr(
            type(telemetry.lithos_metrics),
            "tool_errors",
            property(lambda self: _Recorder()),
        )

        @tool_metrics()
        @tool_span(map_coordination_error=True)
        async def lithos_mapped() -> dict:
            """Demo."""
            raise CoordinationError("cycle", "mapped, not counted")

        result = await lithos_mapped()
        assert result["status"] == "error"
        assert calls == [], "mapped CoordinationError must not increment tool_errors"

        @tool_metrics()
        @tool_span()
        async def lithos_raises() -> dict:
            """Demo."""
            raise RuntimeError("counted")

        with pytest.raises(RuntimeError):
            await lithos_raises()
        assert len(calls) == 1, "raised exceptions must increment tool_errors"
        assert calls[0][1].get("error_type") == "RuntimeError"
