"""Generate per-component drill-down pages under docs/generated/components/.

One page per component: its description, tier, modules (with a coarse size band
so ordinary line churn doesn't dirty the page), the public API of each module,
which components it depends on / is used by, the data stores it owns (when the
[containers] adapter is configured), and the ADRs that mention it. All derived
statically from the code + config.
"""

from __future__ import annotations

import ast
import pathlib

from tests.guardrail._common import (
    REPO_ROOT,
    SRC_ROOT,
    component_of,
    load_architecture,
    module_name_of,
    with_header,
)
from tests.guardrail._diagram_toolkit import component_edges

ADR_DIR = REPO_ROOT / "docs" / "adr"

# Size bands keep the pages stable against ordinary line churn (raw LOC would
# dirty a page on nearly every edit); the metrics snapshot carries exact counts.
_BANDS = ((100, "XS"), (300, "S"), (700, "M"), (1500, "L"))


def _band(lines: int) -> str:
    for limit, name in _BANDS:
        if lines < limit:
            return name
    return "XL"


def component_modules(
    components: dict[str, list[str]],
) -> dict[str, list[pathlib.Path]]:
    out: dict[str, list[pathlib.Path]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        comp = component_of(module_name_of(path), components)
        if comp is not None:
            out.setdefault(comp, []).append(path)
    return out


def _public_api(tree: ast.Module) -> list[tuple[str, str, str]]:
    """Top-level public (kind, name, summary) — classes and functions."""
    api: list[tuple[str, str, str]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and not (
            node.name.startswith("_")
        ):
            kind = "class" if isinstance(node, ast.ClassDef) else "def"
            doc = ast.get_docstring(node)
            summary = " ".join(doc.split("\n\n", 1)[0].split()) if doc else ""
            api.append((kind, node.name, summary))
    return api


def _adr_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def _adrs_for(paths: list[pathlib.Path]) -> list[tuple[str, str]]:
    """ADRs mentioning any of these modules.

    Matched on the filename (``edge_store.py``) and dotted name
    (``lithos.edge_store``), plus the bare stem when it is a compound identifier
    (``edge_store``) — those are safe from prose false positives, unlike a plain
    word like ``graph`` which would match "knowledge graph" everywhere.
    """
    tokens = {p.name for p in paths} | {module_name_of(p) for p in paths}
    tokens |= {p.stem for p in paths if "_" in p.stem}
    hits: list[tuple[str, str]] = []
    for adr in sorted(ADR_DIR.glob("*.md")):
        text = adr.read_text(encoding="utf-8")
        if any(tok in text for tok in tokens):
            hits.append((adr.name, _adr_title(text, adr.stem)))
    return hits


def _tier_of(component: str, tiers: dict[str, list[str]]) -> str:
    return next((tier for tier, members in tiers.items() if component in members), "")


def _owned_stores(component: str, arch: dict) -> list[dict]:
    """Data stores owned by this component — [] unless [containers] is configured.

    The _containers import is local and only reachable when the config section
    is populated, so projects without the containers adapter (and without the
    _containers module) still run this generator unchanged.
    """
    if not arch.get("containers", {}).get("stores"):
        return []
    # The module only exists in repos with the containers adapter installed
    # (short import line so 88-width reflow can't strand the ignore comment).
    from tests.guardrail import _containers  # pyright: ignore

    return [st for st in _containers.stores() if st["owner"] == component]


def render_component_page(
    component: str,
    arch: dict,
    edges: set[tuple[str, str]],
    comp_modules: dict[str, list[pathlib.Path]],
) -> str:
    desc = arch.get("component_docs", {}).get(component, "")
    tier = _tier_of(component, arch.get("tiers", {}))
    paths = comp_modules.get(component, [])
    depends = sorted({d for s, d in edges if s == component})
    used_by = sorted({s for s, d in edges if d == component})
    owned = _owned_stores(component, arch)

    lines = [f"# {component}", ""]
    if desc:
        lines += [desc, ""]
    if tier:
        lines += [f"**Tier:** {tier}", ""]

    lines += [
        "## Modules",
        "",
        "| Module | Size | Classes | Functions |",
        "|---|---|---:|---:|",
    ]
    apis: dict[str, list[tuple[str, str, str]]] = {}
    for path in paths:
        module = module_name_of(path)
        text = path.read_text(encoding="utf-8")
        api = _public_api(ast.parse(text, filename=str(path)))
        apis[module] = api
        n_class = sum(1 for k, _, _ in api if k == "class")
        n_def = sum(1 for k, _, _ in api if k == "def")
        lines.append(f"| `{module}` | {_band(len(text.splitlines()))} | {n_class} | {n_def} |")

    lines += ["", "## Public API"]
    for path in paths:
        module = module_name_of(path)
        api = apis[module]
        if not api:
            continue
        lines += ["", f"### `{module}`"]
        for kind, name, summary in api:
            lines.append(f"- {kind} `{name}`" + (f" — {summary}" if summary else ""))

    lines += ["", "## Dependencies", ""]
    lines.append("- Depends on: " + (", ".join(f"[{d}]({d}.md)" for d in depends) or "—"))
    lines.append("- Used by: " + (", ".join(f"[{u}]({u}.md)" for u in used_by) or "—"))

    if owned:
        lines += ["", "## Data stores", ""]
        for st in owned:
            engine = f" ({st['engine']})" if st.get("engine") else ""
            lines.append(f"- `{st['id']}` — {st['label']}{engine}")

    adrs = _adrs_for(paths)
    if adrs:
        lines += ["", "## ADRs", ""]
        for fname, title in adrs:
            lines.append(f"- [{title}](../../adr/{fname})")

    lines += ["", "[← all generated docs](../README.md)"]
    return with_header("\n".join(lines) + "\n")


def render_all() -> dict[str, str]:
    arch = load_architecture()
    components: dict[str, list[str]] = arch["components"]
    edges = component_edges(components)
    comp_modules = component_modules(components)
    return {
        f"components/{c}.md": render_component_page(c, arch, edges, comp_modules)
        for c in sorted(components)
    }
