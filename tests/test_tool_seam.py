"""Unit tests for the tool_span tracing/error-mapping seam."""

import inspect

import pytest

from lithos.errors import AmbiguousIdPrefixError, CoordinationError
from lithos.telemetry import tool_metrics
from lithos.tools._seam import resolve_note_id, tool_span


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

    @pytest.mark.parametrize("map_flag", [True, False])
    async def test_ambiguous_id_prefix_maps_under_both_flag_values(self, map_flag: bool):
        """AmbiguousIdPrefixError maps unconditionally — note tools use the
        bare seam, task tools the mapping one, and both must envelope it."""
        candidates = [{"id": "aaaa-1", "title": "one"}, {"id": "aaaa-2", "title": "two"}]

        @tool_span(map_coordination_error=map_flag)
        async def lithos_demo() -> dict:
            """Demo."""
            raise AmbiguousIdPrefixError("task", "aaaa", candidates, field="task_id")

        result = await lithos_demo()
        assert result["status"] == "error"
        assert result["code"] == "ambiguous_id_prefix"
        assert result["candidates"] == candidates
        assert "task_id" in result["message"]

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

    async def test_resolve_note_id_maps_failures_and_returns_pair(self):
        class _FakeKnowledge:
            def resolve_id(self, raw: str) -> tuple[str, str]:
                if raw == "toosh":
                    raise ValueError("id 'toosh' is too short")
                if raw == "missing-prefix":
                    raise FileNotFoundError("Document not found: missing-prefix")
                return "resolved-full-id", "A Title"

        knowledge = _FakeKnowledge()
        assert resolve_note_id(knowledge, "found-") == ("resolved-full-id", "A Title")
        too_short = resolve_note_id(knowledge, "toosh")
        assert isinstance(too_short, dict) and too_short["code"] == "invalid_input"
        missing = resolve_note_id(knowledge, "missing-prefix", not_found_code="note_not_found")
        assert isinstance(missing, dict) and missing["code"] == "note_not_found"

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
