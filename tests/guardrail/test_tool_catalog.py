"""Regenerate the MCP tool catalog and guard its completeness (drift-checked).

Rewrites ``docs/generated/tool_catalog.md`` from the ``@…tool()`` handlers in
the modules listed under ``[tool_catalog].include_modules``. CI fails if the
committed file drifts from the code.
"""

from __future__ import annotations

from tests.guardrail import _tool_catalog as tc
from tests.guardrail._common import load_architecture, write

_TOOL_PREFIX = "lithos_"


def test_generate_tool_catalog() -> None:
    out = write("tool_catalog.md", tc.render_tool_catalog())
    assert out.exists()
    assert "# MCP tool catalog" in out.read_text(encoding="utf-8")


def test_tools_found_and_named() -> None:
    tools = tc.discover_tools()
    assert len(tools) >= 35, f"expected the full tool surface, found {len(tools)}"
    names = [t.name for t in tools]
    assert len(names) == len(set(names)), "duplicate tool names"
    assert all(n.startswith(_TOOL_PREFIX) for n in names), (
        "all MCP tools must be prefixed; offenders: "
        + ", ".join(n for n in names if not n.startswith(_TOOL_PREFIX))
    )


def test_every_tool_has_a_summary() -> None:
    missing = [t.name for t in tc.discover_tools() if not t.summary]
    assert not missing, f"tools without a docstring summary: {missing}"


def test_all_touched_server_attrs_are_classified() -> None:
    """A handler reaching an unmapped, non-ignored server attr must fail here.

    This is the completeness guard: adding a new service and wiring a tool to it
    without updating ``[tool_catalog]`` surfaces as a listed failure.
    """
    cfg = load_architecture().get("tool_catalog", {})
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
