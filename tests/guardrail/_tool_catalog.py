"""Generate the MCP tool-surface catalog from the code (drift-checked).

The server's public API is ~40 ``@mcp.tool()``-decorated handlers spread across
the modules listed in ``[tool_catalog].include_modules``. This scans them
statically (no imports) and renders ``docs/generated/tool_catalog.md``: per
module, a table of tool / one-line summary / which core components the handler
touches, plus the tool signatures.

"Touches" is derived by walking each handler body for ``server.<attr>`` access
and mapping the attribute through ``[tool_catalog.component_attrs]`` — so a new
handler that reaches into an unclassified service shows up in the completeness
test until the mapping is updated.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from tests.guardrail._common import load_architecture, module_files, module_name_of, with_header

DEFAULT_CLOSURE_VAR = "server"  # register(mcp, server): handlers close over this name


@dataclass(frozen=True)
class ToolInfo:
    name: str
    module: str
    summary: str
    signature: str
    components: tuple[str, ...]
    server_attrs: tuple[str, ...]  # public server.<attr> used (for the completeness check)


def _has_tool_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in node.decorator_list:
        func = dec.func if isinstance(dec, ast.Call) else dec
        if ast.unparse(func).endswith(".tool"):
            return True
    return False


def _summary(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    doc = ast.get_docstring(node)
    if not doc:
        return ""
    first = doc.split("\n\n", 1)[0]
    return " ".join(first.split())


def _render_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    a = node.args
    parts: list[str] = []

    positional = a.posonlyargs + a.args
    defaults = list(a.defaults)
    pad = [None] * (len(positional) - len(defaults))
    for arg, default in zip(positional, pad + defaults, strict=True):
        parts.append(_render_arg(arg, default))

    for arg, default in zip(a.kwonlyargs, a.kw_defaults, strict=True):
        parts.append(_render_arg(arg, default))

    return f"{node.name}({', '.join(parts)})"


def _render_arg(arg: ast.arg, default: ast.expr | None) -> str:
    text = arg.arg
    if arg.annotation is not None:
        text += f": {ast.unparse(arg.annotation)}"
    if default is not None:
        sep = " = " if arg.annotation is not None else "="
        text += f"{sep}{ast.unparse(default)}"
    return text


def _server_attrs(node: ast.AST, closure_var: str) -> set[str]:
    """Public ``<closure_var>.<attr>`` names accessed in a handler body."""
    return {
        n.attr
        for n in ast.walk(node)
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id == closure_var
    }


def _components(attrs: set[str], attr_map: dict[str, str]) -> tuple[str, ...]:
    return tuple(sorted({attr_map[a] for a in attrs if a in attr_map}))


def discover_tools() -> list[ToolInfo]:
    cfg = load_architecture().get("tool_catalog", {})
    include = cfg.get("include_modules", [])
    attr_map: dict[str, str] = cfg.get("component_attrs", {})
    closure_var = cfg.get("closure_var", DEFAULT_CLOSURE_VAR)

    tools: list[ToolInfo] = []
    for path in module_files(include):
        module = module_name_of(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not _has_tool_decorator(node):
                continue
            attrs = _server_attrs(node, closure_var)
            tools.append(
                ToolInfo(
                    name=node.name,
                    module=module,
                    summary=_summary(node),
                    signature=_render_signature(node),
                    components=_components(attrs, attr_map),
                    server_attrs=tuple(sorted(a for a in attrs if not a.startswith("_"))),
                )
            )
    return sorted(tools, key=lambda t: (t.module, t.name))


def _heading(module: str) -> str:
    return module.split(".")[-1].replace("_", " ").title()


def render_tool_catalog() -> str:
    tools = discover_tools()
    closure_var = (
        load_architecture().get("tool_catalog", {}).get("closure_var", DEFAULT_CLOSURE_VAR)
    )
    lines = [
        "# MCP tool catalog",
        "",
        f"{len(tools)} tools exposed by the server, grouped by source module. "
        '"Touches" lists the core components each handler reaches (via '
        f"`{closure_var}.<attr>`). Generated from the code — see `docs/architecture.toml` "
        "`[tool_catalog]`.",
    ]

    modules = sorted({t.module for t in tools})
    for module in modules:
        group = [t for t in tools if t.module == module]
        lines += [
            "",
            f"## {_heading(module)}",
            f"`{module}`",
            "",
            "| Tool | Summary | Touches |",
            "|---|---|---|",
        ]
        for t in group:
            touches = ", ".join(t.components) if t.components else "—"
            summary = t.summary or "—"
            lines.append(f"| `{t.name}` | {summary} | {touches} |")
        lines += ["", "```text"]
        lines += [t.signature for t in group]
        lines.append("```")

    return with_header("\n".join(lines) + "\n")
