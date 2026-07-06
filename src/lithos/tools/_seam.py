"""Tracing + error-mapping seam for MCP tool handlers.

One decorator owns the per-tool span convention and the
``CoordinationError`` → canonical-envelope mapping, replacing the
hand-rolled preamble every handler used to restate.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any

from lithos.envelopes import coordination_error_envelope
from lithos.errors import CoordinationError
from lithos.telemetry import get_tracer

AsyncHandler = Callable[..., Awaitable[Any]]


def tool_span(*, map_coordination_error: bool = False) -> Callable[[AsyncHandler], AsyncHandler]:
    """Run the handler inside a ``lithos.tool.<short>`` span.

    Sets the ``lithos.tool`` attribute from the handler's ``__name__``;
    handler bodies set per-tool attributes via
    :func:`lithos.telemetry.get_current_span`.

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

    def decorator(func: AsyncHandler) -> AsyncHandler:
        tool_name = func.__name__
        span_name = f"lithos.tool.{tool_name.removeprefix('lithos_')}"

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = get_tracer()
            with tracer.start_as_current_span(span_name) as span:
                span.set_attribute("lithos.tool", tool_name)
                if not map_coordination_error:
                    return await func(*args, **kwargs)
                try:
                    return await func(*args, **kwargs)
                except CoordinationError as exc:
                    span.set_attribute("lithos.success", False)
                    return coordination_error_envelope(exc)

        return wrapper

    return decorator
