"""Render the metrics dict to ``metrics.json`` (machine) and ``metrics.md`` (human).

Both renderers consume the single dict produced by
:func:`tests.guardrail._metrics_toolkit.compute_metrics`, so the two committed
artifacts can never disagree with each other.
"""

from __future__ import annotations

import json
from typing import Any

from tests.guardrail._common import LANGUAGE, load_architecture, with_header
from tests.guardrail._metrics_toolkit import COMPLEXITY_THRESHOLD, GOD_MODULE_LINES, budget_actual
from tests.guardrail._private_access import DETAIL_CAP


def render_metrics_json(metrics: dict[str, Any]) -> str:
    return json.dumps(metrics, indent=2, sort_keys=True) + "\n"


def _budget_table(metrics: dict[str, Any], budgets: dict[str, int]) -> list[str]:
    lines = [
        "## Budgets",
        "",
        "Hard limits from `docs/architecture.toml [budgets]`, enforced in CI by",
        "`tests/guardrail/test_metrics_budgets.py`. Headroom = budget - actual;",
        "lower a budget after improving the code to lock in the gain.",
        "",
        "| Metric | Actual | Budget | Headroom |",
        "|---|---:|---:|---:|",
    ]
    for key in sorted(budgets):
        actual = budget_actual(metrics, key)
        lines.append(f"| `{key}` | {actual} | {budgets[key]} | {budgets[key] - actual} |")
    return lines


def _component_table(metrics: dict[str, Any]) -> list[str]:
    lines = [
        "## Components",
        "",
        "Instability I = fan-out / (fan-in + fan-out): 0 = stable (many dependents),",
        "1 = unstable (depends on many, nothing depends on it).",
        "",
        "| Component | Modules | Lines | SLOC | Fan-in | Fan-out | Instability |"
        " Max complexity | Functions > "
        f"{COMPLEXITY_THRESHOLD} |",
        "|---|---:|---:|---:|---:|---:|---:|---|---:|",
    ]
    graph = metrics["graph"]["components"]
    size = metrics["size"]["components"]
    cx = metrics["complexity"]["components"]
    for comp in sorted(size):
        g, s, c = graph[comp], size[comp], cx[comp]
        max_cx = f"{c['max_complexity']} (`{c['max_function']}`)" if c["max_function"] else "-"
        lines.append(
            f"| {comp} | {s['modules']} | {s['lines']} | {s['sloc']} | {g['fan_in']} |"
            f" {g['fan_out']} | {g['instability']:.2f} | {max_cx} | {c['functions_over_10']} |"
        )
    return lines


def _graph_section(metrics: dict[str, Any]) -> list[str]:
    g = metrics["graph"]
    cycles = (
        "; ".join(" ↔ ".join(scc) for scc in g["component_cycles"])
        if g["component_cycles"]
        else "none"
    )
    module_cycles = (
        "; ".join(" ↔ ".join(scc) for scc in g["module_cycles"]) if g["module_cycles"] else "none"
    )
    skips = ", ".join(g["tier_skipping"]) if g["tier_skipping"] else "none"
    # Name the span in the repo's own tier vocabulary (a skip = any edge
    # jumping ≥2 tiers, illustrated by the top-to-bottom extreme).
    tiers = list(load_architecture().get("tiers", {})) or ["Entrypoints", "Core", "Foundation"]
    return [
        "## Import graph",
        "",
        f"- Cross-component edges: **{g['cross_component_edges']}**"
        f" ({g['cross_component_module_edges']} module-level)",
        f"- Component cycles: {cycles}",
        f"- Module cycles: {module_cycles}",
        f"- Tier-skipping edges ({tiers[0]} → {tiers[-1]}): {g['tier_skipping_edges']} ({skips})",
        f"- Longest component dependency chain: {g['longest_component_chain']}",
    ]


def _size_section(metrics: dict[str, Any]) -> list[str]:
    s = metrics["size"]
    lines = [
        "## Size",
        "",
        f"- Modules: **{s['total_modules']}**, lines: **{s['total_lines']}**,"
        f" SLOC: **{s['total_sloc']}**",
        f"- Largest module: `{s['max_module']}` ({s['max_module_lines']} lines)",
        f"- Modules over {GOD_MODULE_LINES} lines: **{len(s['modules_over_800'])}**",
    ]
    lines.extend(f"  - `{m}`" for m in s["modules_over_800"])
    return lines


def _complexity_section(metrics: dict[str, Any]) -> list[str]:
    c = metrics["complexity"]
    lines = [
        "## Complexity",
        "",
        f"- Functions: **{c['total_functions']}**, cyclomatic > {COMPLEXITY_THRESHOLD}:"
        f" **{c['functions_over_10']}**",
        "",
        "Top 10 most complex functions:",
        "",
        "| Complexity | Function |",
        "|---:|---|",
    ]
    lines.extend(f"| {t['complexity']} | `{t['qualname']}` |" for t in c["top_functions"])
    return lines


def _seam_details(lines: list[str], details: list[str]) -> None:
    lines.extend(f"  - `{d}`" for d in details)
    if len(details) == DETAIL_CAP:
        lines.append(f"  - … (list capped at {DETAIL_CAP} pairs)")


def _seams_section(metrics: dict[str, Any]) -> list[str]:
    s = metrics["seams"]
    lines = [
        "## Seams",
        "",
        "Private-name reaches across module seams. Both counts can be pinned as",
        "`[budgets]` ratchets (`cross_module_private_refs`, `tests_private_imports`).",
        "",
    ]
    if LANGUAGE != "python":
        lines.append("Not scanned: underscore privacy is a Python convention (n/a here).")
        return lines
    lines.append(f"- Cross-module private refs (src): **{s['cross_module_private_refs']}**")
    _seam_details(lines, s["cross_module_private_detail"])
    lines.append(f"- Tests importing src privates: **{s['tests_private_imports']}**")
    _seam_details(lines, s["tests_private_detail"])
    return lines


def _summary_section(metrics: dict[str, Any]) -> list[str]:
    d, m, t = metrics["domain"], metrics["mcp"], metrics["tests"]
    # Optional surfaces: report the MCP tool catalog only when this project
    # declares one, and the domain model only for Python (it is derived from
    # dataclasses / Pydantic models), so metrics.md never advertises a zero.
    has_tools = bool(load_architecture().get("tool_catalog", {}).get("include_modules"))
    has_domain = LANGUAGE == "python"
    if has_tools:
        title = "## Domain, tools & tests"
    elif has_domain:
        title = "## Domain & tests"
    else:
        title = "## Tests"
    lines = [title, ""]
    if has_domain:
        lines.append(
            f"- Domain models: **{d['models']}** ({d['associations']} associations,"
            f" {d['models_without_docstrings']} without docstrings)"
        )
    if has_tools:
        lines.append(
            f"- MCP tools: **{m['tools']}** ({m['tools_without_docstrings']} without docstrings)"
        )
    lines.append(
        f"- Test-to-source line ratio: **{t['ratio']:.2f}**"
        f" ({t['test_lines']} test lines / {t['src_lines']} source lines)"
    )
    return lines


def render_metrics_md(metrics: dict[str, Any], budgets: dict[str, int]) -> str:
    sections = [
        ["# Architecture metrics"],
        _budget_table(metrics, budgets),
        _graph_section(metrics),
        _component_table(metrics),
        _size_section(metrics),
        _complexity_section(metrics),
        _seams_section(metrics),
        _summary_section(metrics),
    ]
    body = "\n\n".join("\n".join(section) for section in sections)
    return with_header(body + "\n")
