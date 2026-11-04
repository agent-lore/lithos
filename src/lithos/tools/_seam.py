"""Tracing + error-mapping seam for MCP tool handlers.

One decorator owns the per-tool span convention — the span itself, the
well-known argument tags, the entry log line — and the ``CoordinationError``
→ canonical-envelope mapping, replacing the hand-rolled preamble every
handler used to restate (and drift on).
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from lithos.envelopes import coordination_error_envelope, error_envelope, invalid_input_envelope
from lithos.errors import AmbiguousIdPrefixError, CoordinationError
from lithos.telemetry import get_current_span, get_tracer

AsyncHandler = Callable[..., Awaitable[Any]]

# Handler arguments every dashboard keys on, tagged by the seam at entry under
# the attribute names the observability stack has always used. Order is the
# order they appear in the entry log line.
WELL_KNOWN_ARGS: tuple[tuple[str, str], ...] = (
    ("agent", "lithos.agent"),
    ("task_id", "lithos.task_id"),
    ("aspect", "lithos.aspect"),
)


class _NoteResolver(Protocol):
    def resolve_id(self, raw: str) -> tuple[str, str]: ...


class _TaskResolver(Protocol):
    async def resolve_task_id(
        self, raw: str, *, field: str = "task_id"
    ) -> tuple[str, str | None]: ...


@dataclass(frozen=True)
class _Preamble:
    """What the seam does at entry for one handler, fixed at decoration time:
    which arguments it tags, which it logs, and on whose logger."""

    tool_name: str
    signature: inspect.Signature
    logged: tuple[str, ...]
    logger: logging.Logger

    @classmethod
    def for_handler(cls, func: AsyncHandler, log_args: tuple[str, ...]) -> _Preamble:
        signature = inspect.signature(func)
        unknown = [name for name in log_args if name not in signature.parameters]
        if unknown:
            raise TypeError(f"{func.__name__}: log_args name no parameter: {', '.join(unknown)}")
        well_known = [name for name, _ in WELL_KNOWN_ARGS]
        logged = tuple(well_known + [name for name in log_args if name not in well_known])
        return cls(func.__name__, signature, logged, logging.getLogger(func.__module__))

    def enter(self, span: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        """Tag the well-known arguments present and emit the entry log line."""
        bound = self.signature.bind_partial(*args, **kwargs)
        bound.apply_defaults()
        present = {k: v for k, v in bound.arguments.items() if v is not None}
        span.set_attribute("lithos.tool", self.tool_name)
        for name, attribute in WELL_KNOWN_ARGS:
            if name in present:
                span.set_attribute(attribute, present[name])
        fields = [f"{name}={present[name]}" for name in self.logged if name in present]
        self.logger.info("%s", " ".join([self.tool_name, *fields]))


def tool_span(
    *, map_coordination_error: bool = False, log_args: Iterable[str] = ()
) -> Callable[[AsyncHandler], AsyncHandler]:
    """Run the handler inside a ``lithos.tool.<short>`` span.

    Sets the ``lithos.tool`` attribute from the handler's ``__name__`` and, at
    entry, tags the well-known arguments (:data:`WELL_KNOWN_ARGS`) that were
    supplied and are not ``None`` — ``lithos.agent``, ``lithos.task_id``,
    ``lithos.aspect`` — however the handler was called (positionally or by
    keyword). A ``task_id`` is tagged as given; :func:`resolve_task_id`
    replaces it with the full id once resolved. Handler bodies set only
    tool-specific attributes via :func:`lithos.telemetry.get_current_span`.

    One entry log line, ``<tool> key=value ...``, goes to the handler module's
    logger (``logging.getLogger(func.__module__)``) naming the well-known
    arguments present plus ``log_args`` — argument names the handler wants in
    the line (scalars; never bodies or metadata). Unknown names fail here, at
    decoration time.

    With ``map_coordination_error=True``, a raised
    :class:`~lithos.errors.CoordinationError` is mapped onto the canonical
    error envelope and returned (marking ``lithos.success`` false on the
    span).

    Stack order is load-bearing::

        @mcp.tool()
        @tool_metrics()
        @tool_span()

    Below ``tool_metrics`` means a raised exception still traverses the span
    exit and is then counted as a tool error, while a mapped
    ``CoordinationError`` returns normally and is NOT counted — matching the
    per-handler behaviour this decorator replaces.
    """
    extra_log_args = tuple(log_args)

    def decorator(func: AsyncHandler) -> AsyncHandler:
        preamble = _Preamble.for_handler(func, extra_log_args)
        span_name = f"lithos.tool.{preamble.tool_name.removeprefix('lithos_')}"

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = get_tracer()
            with tracer.start_as_current_span(span_name) as span:
                preamble.enter(span, args, kwargs)
                try:
                    return await func(*args, **kwargs)
                except AmbiguousIdPrefixError as exc:
                    # Mapped unconditionally: raised only by the id resolvers,
                    # and never silently picked (task 83257ced).
                    span.set_attribute("lithos.success", False)
                    return error_envelope(
                        "ambiguous_id_prefix", exc.message, candidates=exc.candidates
                    )
                except CoordinationError as exc:
                    if not map_coordination_error:
                        raise
                    span.set_attribute("lithos.success", False)
                    return coordination_error_envelope(exc)

        return wrapper

    return decorator


async def resolve_task_id(
    coordination: _TaskResolver, raw: str, *, field: str = "task_id"
) -> tuple[str, str | None]:
    """Resolve a task id (or prefix) for a handler and re-tag the span.

    Returns ``(full_id, title)`` from the coordination resolver and replaces
    the ``lithos.task_id`` the seam tagged at entry with the full id, so a
    span never ends on a display prefix. The loud failures
    (``task_not_found``, ``invalid_input``, ambiguity) propagate as they do
    from the resolver and are mapped by :func:`tool_span`.
    """
    task_id, title = await coordination.resolve_task_id(raw, field=field)
    get_current_span().set_attribute("lithos.task_id", task_id)
    return task_id, title


def resolve_note_id(
    knowledge: _NoteResolver, raw: str, *, not_found_code: str = "doc_not_found"
) -> tuple[str, str] | dict[str, Any]:
    """Resolve a note id (or prefix) for a handler, mapping the loud failures.

    Returns the ``(full_id, title)`` pair on success, or the canonical error
    envelope the handler should return as-is — each note tool keeps its
    historical not-found code via ``not_found_code``. Ambiguity propagates to
    :func:`tool_span`, which owns that mapping.
    """
    try:
        return knowledge.resolve_id(raw)
    except ValueError as exc:
        return invalid_input_envelope(str(exc))
    except FileNotFoundError as exc:
        return error_envelope(not_found_code, str(exc))
