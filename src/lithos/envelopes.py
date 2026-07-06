"""Canonical MCP error-envelope constructors.

The canonical failure shape is built here::

    {"status": "error", "code": "<stable_snake_case>", "message": "<sentence>"}

``code`` is machine-stable — agents branch on it and never parse ``message``.
Key order is part of the wire contract (dicts serialise in insertion order);
do not reorder.

Every MCP tool failure uses this one wire shape; validation failures carry
the reserved code ``invalid_input`` and error envelopes never include
``warnings``. Handlers build failures through these constructors — a few
Core modules still assemble the same shape inline and are migrating.

Success envelopes remain per-tool: they are the tool's result shape, not a
shared failure contract. Actionable write *outcomes* (``duplicate``,
``slug_collision``, ``path_collision``, ``version_conflict``) are also not
errors — they carry payloads agents act on and keep their own top-level
statuses.
"""

from __future__ import annotations

from typing import Any

from lithos.errors import CoordinationError

_CANONICAL_KEYS = frozenset({"status", "code", "message"})


def error_envelope(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """Build the canonical error envelope.

    ``extra`` allows documented code-specific supplementary keys; they are
    appended after the three canonical keys and may not override them.
    """
    overridden = _CANONICAL_KEYS & extra.keys()
    if overridden:
        raise ValueError(f"extra must not override canonical envelope keys: {sorted(overridden)}")
    return {"status": "error", "code": code, "message": message, **extra}


def invalid_input_envelope(message: str) -> dict[str, Any]:
    """Build the canonical validation-failure envelope.

    Structurally the operational shape with the reserved code
    ``invalid_input``: the request was rejected at the boundary and no side
    effects occurred. One error branch for agents; ``code`` distinguishes
    "fix your input" from "handle a failure".
    """
    return error_envelope("invalid_input", message)


def coordination_error_envelope(exc: CoordinationError) -> dict[str, Any]:
    """Map a :class:`~lithos.errors.CoordinationError` onto the canonical envelope.

    The single construction point for the mapping the exception was designed
    for — handlers must not restate the dict.
    """
    return error_envelope(exc.code, exc.message)
