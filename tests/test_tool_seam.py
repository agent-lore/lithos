"""Unit tests for the tool_span tracing/error-mapping seam."""

import contextlib
import inspect
import logging

import pytest

from lithos.errors import AmbiguousIdPrefixError, CoordinationError
from lithos.telemetry import tool_metrics
from lithos.tools._seam import resolve_note_id, resolve_task_id, tool_span


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


class _RecordingSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


class _RecordingTracer:
    def __init__(self, span: _RecordingSpan) -> None:
        self.span = span
        self.names: list[str] = []

    @contextlib.contextmanager
    def start_as_current_span(self, name: str):
        self.names.append(name)
        yield self.span


@pytest.fixture
def recording_span(monkeypatch) -> _RecordingSpan:
    """Route the seam's tracer and current-span lookups at one recording span."""
    span = _RecordingSpan()
    monkeypatch.setattr("lithos.tools._seam.get_tracer", lambda: _RecordingTracer(span))
    monkeypatch.setattr("lithos.tools._seam.get_current_span", lambda: span)
    return span


class TestToolSpanPreamble:
    """The seam owns the entry preamble (task 8a6c9418): the well-known
    argument tags and the one entry log line every handler used to restate."""

    async def test_tags_well_known_arguments_from_keywords(self, recording_span):
        @tool_span()
        async def lithos_demo(task_id: str, agent: str, aspect: str, title: str) -> dict:
            """Demo."""
            return {}

        await lithos_demo(task_id="t-1", agent="agent-a", aspect="impl", title="not tagged")

        assert recording_span.attributes == {
            "lithos.tool": "lithos_demo",
            "lithos.agent": "agent-a",
            "lithos.task_id": "t-1",
            "lithos.aspect": "impl",
        }

    async def test_tags_well_known_arguments_from_positionals(self, recording_span):
        @tool_span()
        async def lithos_demo(task_id: str, agent: str) -> dict:
            """Demo."""
            return {}

        await lithos_demo("t-1", "agent-a")

        assert recording_span.attributes["lithos.agent"] == "agent-a"
        assert recording_span.attributes["lithos.task_id"] == "t-1"

    async def test_absent_or_none_arguments_are_not_tagged(self, recording_span):
        """``lithos_task_list(agent=None)`` must not tag an agent — the
        attribute means "this agent acted", not "the agent filter was None"."""

        @tool_span()
        async def lithos_demo(agent: str | None = None, limit: int = 5) -> dict:
            """Demo."""
            return {}

        await lithos_demo()

        assert recording_span.attributes == {"lithos.tool": "lithos_demo"}

    async def test_entry_log_line_once_via_the_handlers_module_logger(self, recording_span, caplog):
        """One ``<tool> key=value`` line per call, on the handler module's
        logger (so log routing by module name is unchanged), naming the
        well-known arguments present plus the handler's declared ``log_args``.
        """

        @tool_span(log_args=("title", "limit"))
        async def lithos_demo(
            task_id: str, agent: str, title: str, limit: int = 5, metadata: dict | None = None
        ) -> dict:
            """Demo."""
            return {}

        with caplog.at_level(logging.INFO, logger=__name__):
            await lithos_demo("abc12345", "agent-a", "A title", metadata={"big": "x" * 100})

        records = [r for r in caplog.records if r.name == __name__]
        assert len(records) == 1
        assert records[0].getMessage() == (
            "lithos_demo agent=agent-a task_id=abc12345 title=A title limit=5"
        )

    async def test_entry_log_line_omits_absent_values(self, recording_span, caplog):
        @tool_span(log_args=("status",))
        async def lithos_demo(agent: str | None = None, status: str | None = None) -> dict:
            """Demo."""
            return {}

        with caplog.at_level(logging.INFO, logger=__name__):
            await lithos_demo()

        assert [r.getMessage() for r in caplog.records if r.name == __name__] == ["lithos_demo"]

    def test_unknown_log_args_fail_at_decoration_time(self):
        async def lithos_demo(agent: str) -> dict:
            """Demo."""
            return {}

        with pytest.raises(TypeError, match="no_such_argument"):
            tool_span(log_args=("no_such_argument",))(lithos_demo)

    async def test_resolve_task_id_retags_the_span_with_the_full_id(self, recording_span):
        """The seam tags the raw prefix at entry; resolution replaces it with
        the full id, so the span never ends on a prefix after a successful
        resolve. The title comes back with it for the handler's response."""
        full = "3706b7f7-eaad-4bf3-b460-4bdfc704c921"

        class _FakeCoordination:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            async def resolve_task_id(self, raw: str, *, field: str = "task_id"):
                self.calls.append((raw, field))
                return full, "A task"

        recording_span.set_attribute("lithos.task_id", "3706b7f7")
        coordination = _FakeCoordination()

        resolved = await resolve_task_id(coordination, "3706b7f7", field="parent_task_id")

        assert resolved == (full, "A task")
        assert coordination.calls == [("3706b7f7", "parent_task_id")]
        assert recording_span.attributes["lithos.task_id"] == full
