"""Render the metrics dict to ``metrics.json`` (machine) and ``metrics.md`` (human).

Both renderers consume the single dict produced by
:func:`tests.guardrail._metrics_toolkit.compute_metrics`, so the two committed
artifacts can never disagree with each other. The per-module ``modules`` map
stays JSON-only: a table with one row per module would swamp metrics.md, and
the offenders are already named by ``modules_over_800`` and the component
table — the map exists for trend mining, not for reading.
"""

from __future__ import annotations

import json
from typing import Any

from scripts.quality import policy_mode, render_report, report

from tests.guardrail._common import LANGUAGE, load_architecture, with_header
from tests.guardrail._metrics_toolkit import (
    COMPLEXITY_THRESHOLD,
    FUNCTION_LENGTH_THRESHOLD,
    GOD_MODULE_LINES,
    budget_actual,
)
from tests.guardrail._private_access import DETAIL_CAP


def render_metrics_json(metrics: dict[str, Any]) -> str:
    return json.dumps(metrics, indent=2, sort_keys=True) + "\n"


def _budget_table(metrics: dict[str, Any], budgets: dict[str, int]) -> list[str]:
    lines = [
        "## Budgets",
        "",
        "Only invariant budgets block generation; review/context thresholds are reported.",
        "Accepted budgets from `docs/architecture.toml [budgets]`, enforced in CI by",
        "`tests/guardrail/test_metrics_budgets.py`. Headroom = budget - actual.",
        "",
        "Architectural invariants (dependency restrictions and prohibited cycles)",
        "require an explicit architecture decision to change. Size and complexity",
        "thresholds prompt design review: do not split cohesive code solely to",
        "satisfy a number. If there is no clean seam and keeping the code together",
        "is clearer, adjust the relevant threshold and explain the design tradeoff",
        "in the same PR. Lower thresholds when that protects useful improvements.",
        "",
        "| Metric | Actual | Budget | Headroom | Policy |",
        "|---|---:|---:|---:|---|",
    ]
    for key in sorted(budgets):
        actual = budget_actual(metrics, key)
        lines.append(
            f"| `{key}` | {actual} | {budgets[key]} | {budgets[key] - actual} | {policy_mode(metrics.get('review', {}), key)} |"
        )
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
    fl = c["function_lengths"]
    longest = f" (`{fl['max_function']}`)" if fl["max_function"] else ""
    lines = [
        "## Complexity",
        "",
        f"- Functions: **{c['total_functions']}**, cyclomatic > {COMPLEXITY_THRESHOLD}:"
        f" **{c['functions_over_10']}**",
        f"- Cyclomatic distribution: median **{c['median']}**, p90 **{c['p90']}**,"
        f" max **{c['max']}**",
        f"- Function length (lines): median {fl['median']}, p90 {fl['p90']},"
        + f" max {fl['max']}{longest}; over {FUNCTION_LENGTH_THRESHOLD}: **{fl['over_75']}**",
        "",
        "Top 10 most complex functions:",
        "",
        "| Complexity | Length | Function |",
        "|---:|---:|---|",
    ]
    lines.extend(
        f"| {t['complexity']} | {t['length']} | `{t['qualname']}` |" for t in c["top_functions"]
    )
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
        [
            "## Measurement scope",
            "",
            "```json",
            json.dumps(metrics.get("measurement", {}), indent=2, sort_keys=True),
            "```",
            "",
            "Test/source ratio is a volume statistic, not test effectiveness; kit tooling is excluded.",
        ],
        _budget_table(metrics, budgets),
        [render_report(report(metrics, metrics))],
        [
            "## Quality evidence",
            "",
            "Optional measurements are source-bound; missing or stale evidence is not a score.",
            "",
            "```json",
            json.dumps(metrics.get("quality", {}), indent=2, sort_keys=True),
            "```",
        ],
        _graph_section(metrics),
        _component_table(metrics),
        _size_section(metrics),
        _complexity_section(metrics),
        _seams_section(metrics),
        _summary_section(metrics),
    ]
    body = "\n\n".join("\n".join(section) for section in sections)
    return with_header(body + "\n")
