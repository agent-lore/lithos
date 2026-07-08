"""Regenerate the MCP tool catalog and guard its completeness (drift-checked).

Rewrites ``docs/generated/tool_catalog.md`` from the ``@…tool()`` handlers in
the modules listed under ``[tool_catalog].include_modules``. CI fails if the
committed file drifts from the code.
"""

from __future__ import annotations

import pytest

from tests.guardrail import _tool_catalog as tc
from tests.guardrail._common import load_architecture, write


def _tool_config() -> dict:
    return load_architecture().get("tool_catalog", {})


def _require_tool_surface() -> dict:
    cfg = _tool_config()
    if not cfg.get("include_modules"):
        pytest.skip("no [tool_catalog].include_modules — tool catalog is not enabled")
    return cfg


def test_generate_tool_catalog() -> None:
    _require_tool_surface()
    out = write("tool_catalog.md", tc.render_tool_catalog())
    assert out.exists()
    assert "# MCP tool catalog" in out.read_text(encoding="utf-8")


def test_tools_found_and_named() -> None:
    cfg = _require_tool_surface()
    min_tools = cfg.get("min_tools", 0)
    prefix = cfg.get("tool_prefix", "")

    tools = tc.discover_tools()
    assert len(tools) >= min_tools, f"expected >= {min_tools} tools, found {len(tools)}"
    names = [t.name for t in tools]
    assert len(names) == len(set(names)), "duplicate tool names"
    if prefix:
        offenders = [n for n in names if not n.startswith(prefix)]
        assert not offenders, f"all tools must be prefixed {prefix!r}; offenders: {offenders}"


def test_every_tool_has_a_summary() -> None:
    _require_tool_surface()
    missing = [t.name for t in tc.discover_tools() if not t.summary]
    assert not missing, f"tools without a docstring summary: {missing}"


def test_all_touched_server_attrs_are_classified() -> None:
    """A handler reaching an unmapped, non-ignored server attr must fail here.

    This is the completeness guard: adding a new service and wiring a tool to it
    without updating ``[tool_catalog]`` surfaces as a listed failure.
    """
    cfg = _require_tool_surface()
    known = set(cfg.get("component_attrs", {})) | set(cfg.get("ignore_attrs", []))
    unclassified: dict[str, set[str]] = {}
    for tool in tc.discover_tools():
        extra = {a for a in tool.server_attrs if a not in known}
        if extra:
            unclassified[tool.name] = extra
    assert not unclassified, (
        "server attributes touched by tools but not classified in "
        "docs/architecture.toml [tool_catalog] (map to a component, or add to "
        f"ignore_attrs): {unclassified}"
    )
