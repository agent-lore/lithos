"""Deterministic architecture metrics computed statically from the code.

:func:`compute_metrics` returns one plain dict with every metric; the
renderers in :mod:`tests.guardrail._metrics_render` turn that single dict into
``docs/generated/metrics.json`` (machine) and ``metrics.md`` (human), so the
two files can never disagree.

Determinism contract (what makes the CI drift gate meaningful):

* everything derives from the working tree only — no timestamps, shas, or
  tool versions in the payload;
* every list is explicitly sorted before it reaches the dict;
* ratios are rounded to two decimals.

Hard budgets for a subset of these metrics live in ``docs/architecture.toml``
under ``[budgets]`` and are enforced by ``test_metrics_budgets.py``.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import networkx as nx

from tests.guardrail import _diagram_toolkit as dt
from tests.guardrail._common import (
    REPO_ROOT,
    ROOT_PACKAGE,
    SRC_ROOT,
    build_import_graph,
    component_of,
    load_architecture,
    module_files,
    module_name_of,
)

COMPLEXITY_THRESHOLD = 10
GOD_MODULE_LINES = 800

_SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# Import-graph metrics (grimp + networkx)
# --------------------------------------------------------------------------- #
def _module_edges() -> list[tuple[str, str]]:
    graph = build_import_graph()
    return sorted(
        (module, imported)
        for module in graph.modules
        for imported in graph.find_modules_directly_imported_by(module)
    )


def _tier_rank(tiers: dict[str, list[str]]) -> dict[str, int]:
    """Component -> tier index, in declared (top-to-bottom) toml order."""
    return {comp: rank for rank, members in enumerate(tiers.values()) for comp in members}


def _graph_metrics(components: dict[str, list[str]], tiers: dict[str, list[str]]) -> dict[str, Any]:
    module_edges = _module_edges()
    comp_edges = sorted(
        {
            (src, dst)
            for m, i in module_edges
            if (src := component_of(m, components)) is not None
            and (dst := component_of(i, components)) is not None
            and src != dst
        }
    )

    comp_graph = nx.DiGraph(comp_edges)
    comp_graph.add_nodes_from(components)
    component_cycles = sorted(
        sorted(scc) for scc in nx.strongly_connected_components(comp_graph) if len(scc) > 1
    )

    mod_graph = nx.DiGraph(module_edges)
    module_cycles = sorted(
        sorted(scc) for scc in nx.strongly_connected_components(mod_graph) if len(scc) > 1
    )

    rank = _tier_rank(tiers)
    tier_skipping = sorted(
        (src, dst)
        for src, dst in comp_edges
        if src in rank and dst in rank and rank[dst] - rank[src] >= 2
    )

    condensed = nx.condensation(comp_graph)
    longest_chain = int(nx.dag_longest_path_length(condensed))

    per_component: dict[str, dict[str, Any]] = {}
    for comp in sorted(components):
        fan_out = sum(1 for src, _ in comp_edges if src == comp)
        fan_in = sum(1 for _, dst in comp_edges if dst == comp)
        instability = round(fan_out / (fan_in + fan_out), 2) if fan_in + fan_out else 0.0
        per_component[comp] = {
            "fan_in": fan_in,
            "fan_out": fan_out,
            "instability": instability,
        }

    return {
        "component_cycles": component_cycles,
        "components": per_component,
        "cross_component_edges": len(comp_edges),
        "longest_component_chain": longest_chain,
        "module_cycle_count": len(module_cycles),
        "module_cycles": module_cycles,
        "tier_skipping_edges": len(tier_skipping),
        "tier_skipping": [f"{src} -> {dst}" for src, dst in tier_skipping],
    }


# --------------------------------------------------------------------------- #
# Size / shape metrics (text + ast)
# --------------------------------------------------------------------------- #
def _physical_lines(text: str) -> int:
    return len(text.splitlines())


def _sloc(text: str) -> int:
    """Non-blank lines that are not pure ``#`` comments (docstrings count)."""
    return sum(
        1
        for line in text.splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    )


def _size_metrics(components: dict[str, list[str]]) -> dict[str, Any]:
    per_component: dict[str, dict[str, Any]] = {
        comp: {
            "classes": 0,
            "functions": 0,
            "largest_module": "",
            "largest_module_lines": 0,
            "lines": 0,
            "modules": 0,
            "public_symbols": 0,
            "sloc": 0,
        }
        for comp in sorted(components)
    }
    total_lines = total_sloc = total_modules = 0
    over_800: list[str] = []
    max_module, max_lines = "", 0

    for path in sorted(SRC_ROOT.rglob("*.py")):
        module = module_name_of(path)
        text = path.read_text(encoding="utf-8")
        lines, sloc = _physical_lines(text), _sloc(text)
        total_lines += lines
        total_sloc += sloc
        total_modules += 1
        if lines > GOD_MODULE_LINES:
            over_800.append(module)
        if lines > max_lines or (lines == max_lines and module < max_module):
            max_module, max_lines = module, lines

        comp = component_of(module, components)
        if comp is None:
            continue
        tree = ast.parse(text, filename=str(path))
        stats = per_component[comp]
        stats["modules"] += 1
        stats["lines"] += lines
        stats["sloc"] += sloc
        stats["classes"] += sum(isinstance(n, ast.ClassDef) for n in ast.walk(tree))
        stats["functions"] += sum(
            isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) for n in ast.walk(tree)
        )
        stats["public_symbols"] += sum(
            1
            for n in tree.body
            if isinstance(n, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and not n.name.startswith("_")
        )
        if lines > stats["largest_module_lines"]:
            stats["largest_module"], stats["largest_module_lines"] = module, lines

    return {
        "components": per_component,
        "max_module": max_module,
        "max_module_lines": max_lines,
        "modules_over_800": sorted(over_800),
        "total_lines": total_lines,
        "total_modules": total_modules,
        "total_sloc": total_sloc,
    }


# --------------------------------------------------------------------------- #
# Cyclomatic complexity (homegrown ast visitor; nested defs counted separately)
# --------------------------------------------------------------------------- #
def _iter_shallow(fn: ast.AST):
    """Child nodes of *fn*, not descending into nested function/class scopes."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _cyclomatic(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    score = 1
    for node in _iter_shallow(fn):
        if isinstance(
            node,
            ast.If
            | ast.For
            | ast.AsyncFor
            | ast.While
            | ast.ExceptHandler
            | ast.IfExp
            | ast.Assert,
        ):
            score += 1
        elif isinstance(node, ast.BoolOp):
            score += len(node.values) - 1
        elif isinstance(node, ast.match_case):
            score += 1
        elif isinstance(node, ast.comprehension):
            score += 1 + len(node.ifs)
    return score


def _function_complexities(tree: ast.Module, module: str) -> list[tuple[str, int]]:
    """(qualname, complexity) for every function/method in *module*."""
    results: list[tuple[str, int]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                qualname = f"{prefix}.{child.name}"
                results.append((qualname, _cyclomatic(child)))
                visit(child, qualname)
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}.{child.name}")
            else:
                visit(child, prefix)

    visit(tree, module)
    return results


def _complexity_metrics(components: dict[str, list[str]]) -> dict[str, Any]:
    per_component: dict[str, dict[str, Any]] = {
        comp: {"functions_over_10": 0, "max_complexity": 0, "max_function": ""}
        for comp in sorted(components)
    }
    all_functions: list[tuple[str, int]] = []

    for path in sorted(SRC_ROOT.rglob("*.py")):
        module = module_name_of(path)
        comp = component_of(module, components)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        complexities = _function_complexities(tree, module)
        all_functions.extend(complexities)
        if comp is None:
            continue
        stats = per_component[comp]
        for qualname, score in complexities:
            if score > COMPLEXITY_THRESHOLD:
                stats["functions_over_10"] += 1
            if score > stats["max_complexity"] or (
                score == stats["max_complexity"] and qualname < stats["max_function"]
            ):
                stats["max_complexity"], stats["max_function"] = score, qualname

    top = sorted(all_functions, key=lambda item: (-item[1], item[0]))[:10]
    return {
        "components": per_component,
        "functions_over_10": sum(1 for _, score in all_functions if score > COMPLEXITY_THRESHOLD),
        "top_functions": [{"complexity": score, "qualname": name} for name, score in top],
        "total_functions": len(all_functions),
    }


# --------------------------------------------------------------------------- #
# Domain model / MCP tool surface / test ratio
# --------------------------------------------------------------------------- #
def _domain_metrics() -> dict[str, Any]:
    arch = load_architecture()
    domain = arch.get("domain", {})
    include = domain.get("include_modules") or [ROOT_PACKAGE]
    models = dt._discover_models(dt._module_files(include))
    return {
        "associations": len(dt._associations(models)),
        "models": len(models),
        "models_without_docstrings": sum(1 for m in models if ast.get_docstring(m) is None),
    }


def _tool_files() -> list[pathlib.Path]:
    """Source files scanned for @…tool()-decorated MCP handlers.

    Driven by ``[tool_catalog].include_modules`` so the kit ports to another
    project (or one with no MCP surface -> empty list) without code changes.
    """
    modules = load_architecture().get("tool_catalog", {}).get("include_modules", [])
    return module_files(modules)


def _is_tool_decorator(dec: ast.expr) -> bool:
    func = dec.func if isinstance(dec, ast.Call) else dec
    return ast.unparse(func).endswith(".tool")


def _mcp_metrics() -> dict[str, Any]:
    tools = 0
    without_docstrings = 0
    for path in _tool_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
                _is_tool_decorator(dec) for dec in node.decorator_list
            ):
                tools += 1
                if ast.get_docstring(node) is None:
                    without_docstrings += 1
    return {"tools": tools, "tools_without_docstrings": without_docstrings}


def _test_metrics(src_lines: int) -> dict[str, Any]:
    tests_root = REPO_ROOT / "tests"
    test_lines = sum(
        _physical_lines(p.read_text(encoding="utf-8")) for p in sorted(tests_root.rglob("*.py"))
    )
    return {
        "ratio": round(test_lines / src_lines, 2) if src_lines else 0.0,
        "src_lines": src_lines,
        "test_lines": test_lines,
    }


# --------------------------------------------------------------------------- #
def compute_metrics() -> dict[str, Any]:
    arch = load_architecture()
    components: dict[str, list[str]] = arch["components"]
    tiers: dict[str, list[str]] = arch.get("tiers", {})

    size = _size_metrics(components)
    return {
        "complexity": _complexity_metrics(components),
        "domain": _domain_metrics(),
        "graph": _graph_metrics(components, tiers),
        "mcp": _mcp_metrics(),
        "schema": _SCHEMA_VERSION,
        "size": size,
        "tests": _test_metrics(size["total_lines"]),
    }


def budget_actual(metrics: dict[str, Any], key: str) -> int:
    """Measured value for a ``[budgets]`` key. Raises KeyError for unknown keys."""
    actuals = {
        "component_cycles": len(metrics["graph"]["component_cycles"]),
        "cross_component_edges": metrics["graph"]["cross_component_edges"],
        "max_module_lines": metrics["size"]["max_module_lines"],
        "module_cycles": metrics["graph"]["module_cycle_count"],
        "modules_over_800_lines": len(metrics["size"]["modules_over_800"]),
    }
    return actuals[key]
