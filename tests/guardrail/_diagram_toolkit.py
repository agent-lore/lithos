"""Deterministic Mermaid diagram generation from the real project code.

Two generators, both driven by ``docs/architecture.toml``:

* :func:`render_domain_model` — a class diagram of the domain dataclasses /
  Pydantic models, extracted statically with :mod:`ast` (no imports, no side
  effects). Cardinalities are derived from field annotations
  (``list[X]`` -> ``0..*``, ``X | None`` -> ``0..1``, bare ``X`` -> ``1``).
* :func:`render_component_diagram` — a component dependency graph computed from
  the *real* import graph via :mod:`grimp`, grouped into the components declared
  in ``architecture.toml``.

Both emit fenced ```mermaid markdown so GitHub and Obsidian render them inline.
Everything is sorted so identical code always yields byte-identical output —
that is what makes the CI drift check meaningful.

Shared plumbing (paths, config loader, component mapping, grimp graph, writer)
lives in :mod:`tests.guardrail._common` and is re-exported here for the driver
tests.
"""

from __future__ import annotations

import ast
import pathlib
import re
from dataclasses import dataclass

from tests.guardrail._common import (
    ARCH_TOML,
    GENERATED_DIR,
    REPO_ROOT,
    ROOT_PACKAGE,
    SRC_ROOT,
    build_import_graph,
    component_of,
    load_architecture,
    module_files,
    module_name_of,
    with_header,
    write,
)

__all__ = [
    "ARCH_TOML",
    "GENERATED_DIR",
    "REPO_ROOT",
    "ROOT_PACKAGE",
    "SRC_ROOT",
    "component_edges",
    "component_of",
    "load_architecture",
    "render_component_diagram",
    "render_domain_model",
    "write",
]


# --------------------------------------------------------------------------- #
# Component diagram (grimp = real import graph)
# --------------------------------------------------------------------------- #
def component_edges(components: dict[str, list[str]]) -> set[tuple[str, str]]:
    graph = build_import_graph()
    edges: set[tuple[str, str]] = set()
    for module in graph.modules:
        src = component_of(module, components)
        if src is None:
            continue
        for imported in graph.find_modules_directly_imported_by(module):
            dst = component_of(imported, components)
            if dst is not None and dst != src:
                edges.add((src, dst))
    return edges


def render_component_diagram() -> str:
    arch = load_architecture()
    components: dict[str, list[str]] = arch["components"]
    tiers: dict[str, list[str]] = arch.get("tiers", {})
    edges = sorted(component_edges(components))

    # tier rank (declared order) drives edge styling; the lowest tier is the
    # foundation whose heavy fan-in we de-emphasize.
    rank = {c: i for i, members in enumerate(tiers.values()) for c in members}
    lowest = {c for c, r in rank.items() if r == max(rank.values(), default=-1)}

    lines = ["```mermaid", "graph TD"]
    # group nodes into tier subgraphs, in [tiers] DECLARATION order so the
    # diagram reads top-down the way the architecture is documented
    # (Entrypoints -> Core -> Foundation); members sorted within each tier.
    placed: set[str] = set()
    for tier, tier_members in tiers.items():
        members = [c for c in sorted(tier_members) if c in components]
        if not members:
            continue
        # Give the subgraph an explicit id distinct from any component node id.
        # A tier and a component may share a name (e.g. "Entrypoints"); emitting
        # `subgraph Entrypoints` around a node `Entrypoints` makes Mermaid think
        # the subgraph contains itself ("would create a cycle") and GitHub fails
        # to render it. `subgraph tier_X["X"]` keeps the label but avoids the clash.
        tier_id = "tier_" + re.sub(r"\W+", "_", tier)
        lines.append(f'  subgraph {tier_id}["{tier}"]')
        for comp in members:
            lines.append(f"    {comp}")
            placed.add(comp)
        lines.append("  end")
    for comp in sorted(components):
        if comp not in placed:
            lines.append(f"  {comp}")
    # each node links to its drill-down page
    for comp in sorted(components):
        lines.append(f'  click {comp} "components/{comp}.md"')
    # edges, then linkStyle by edge index (indices are stable — edges are sorted)
    styles: list[str] = []
    for i, (src, dst) in enumerate(edges):
        lines.append(f"  {src} --> {dst}")
        if src in rank and dst in rank and rank[dst] - rank[src] >= 2:
            styles.append(f"  linkStyle {i} stroke:#999,stroke-dasharray:4")  # tier-skipping
        elif dst in lowest:
            styles.append(f"  linkStyle {i} stroke:#bbb")  # foundation fan-in, de-emphasized
    lines.extend(styles)
    lines.append("```")
    return with_header("# Component dependencies\n\n" + "\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# Domain class diagram (ast = static source scan)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Assoc:
    src: str
    card: str
    dst: str
    label: str


@dataclass(frozen=True)
class _Model:
    node: ast.ClassDef
    module: str

    @property
    def name(self) -> str:
        return self.node.name


def _module_files(modules: list[str]) -> list[pathlib.Path]:
    return module_files(modules)


def _excluded(module: str, exclude: list[str]) -> bool:
    """True if *module* is, or lives under, any excluded module prefix."""
    return any(module == e or module.startswith(e + ".") for e in exclude)


def _is_dataclass(node: ast.ClassDef) -> bool:
    for dec in node.decorator_list:
        func = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(func, ast.Name) and func.id == "dataclass":
            return True
        if isinstance(func, ast.Attribute) and func.attr == "dataclass":
            return True
    return False


def _is_pydantic(node: ast.ClassDef, model_names: set[str]) -> bool:
    for base in node.bases:
        name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
        if name == "BaseModel" or name in model_names:
            return True
    return False


def _scan_models(files: list[pathlib.Path]) -> list[tuple[ast.ClassDef, str]]:
    """Every public dataclass / Pydantic model as ``(ClassDef, module)``, no dedup."""
    all_classes: list[tuple[ast.ClassDef, str]] = []
    for path in files:
        module = module_name_of(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        all_classes.extend((n, module) for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
    names = {c.name for c, _ in all_classes}
    return [
        (c, mod)
        for c, mod in all_classes
        if (_is_dataclass(c) or _is_pydantic(c, names)) and not c.name.startswith("_")
    ]


def _discover_models(files: list[pathlib.Path]) -> list[_Model]:
    # de-dupe by name (keep first, sorted by name for determinism). Same-named
    # models sharing a Mermaid id would collapse — duplicate_model_names() guards
    # against that so the collapse can't happen silently.
    seen: set[str] = set()
    unique: list[_Model] = []
    for c, mod in sorted(_scan_models(files), key=lambda cm: cm[0].name):
        if c.name not in seen:
            seen.add(c.name)
            unique.append(_Model(node=c, module=mod))
    return unique


def domain_files() -> list[pathlib.Path]:
    """The source files scanned for the domain diagram (include minus exclude)."""
    domain = load_architecture().get("domain", {})
    include = domain.get("include_modules") or [ROOT_PACKAGE]
    exclude = domain.get("exclude_modules", [])
    return [f for f in _module_files(include) if not _excluded(module_name_of(f), exclude)]


def duplicate_model_names(files: list[pathlib.Path]) -> dict[str, list[str]]:
    """Public model names defined in more than one of *files* → the owning modules.

    These would collapse to a single node in the diagram (and share a Mermaid id),
    so a guard test asserts this is empty.
    """
    by_name: dict[str, set[str]] = {}
    for c, mod in _scan_models(files):
        by_name.setdefault(c.name, set()).add(mod)
    return {name: sorted(mods) for name, mods in sorted(by_name.items()) if len(mods) > 1}


def discover_all_models() -> list[_Model]:
    """Every public dataclass / Pydantic model under the source root."""
    return _discover_models(sorted(SRC_ROOT.rglob("*.py")))


def _fields(cls: ast.ClassDef) -> list[tuple[str, ast.expr]]:
    """Public annotated fields as ``(name, annotation-AST)`` pairs."""
    out: list[tuple[str, ast.expr]] = []
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            fname = stmt.target.id
            if fname.startswith("_"):
                continue
            out.append((fname, stmt.annotation))
    return out


# --- structure-aware association extraction (walks the annotation AST) -------- #
_MANY_CTORS = {
    "list", "set", "frozenset", "tuple", "Sequence", "MutableSequence",
    "Iterable", "Collection", "MutableSet",
}  # fmt: skip
_MAP_CTORS = {"dict", "Dict", "Mapping", "MutableMapping", "defaultdict", "OrderedDict"}
_CARD_RANK = {"1": 0, "0..1": 1, "0..*": 2}


def _looser(a: str, b: str) -> str:
    return a if _CARD_RANK[a] >= _CARD_RANK[b] else b


def _anno_head(value: ast.expr) -> str:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return ""


def _slice_elts(sl: ast.expr) -> list[ast.expr]:
    return list(sl.elts) if isinstance(sl, ast.Tuple) else [sl]


def _is_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _walk_anno(
    anno: ast.expr, model_names: set[str], card: str, out: list[tuple[str, str]]
) -> None:
    if isinstance(anno, ast.Name):
        if anno.id in model_names:
            out.append((anno.id, card))
    elif isinstance(anno, ast.Attribute):
        if anno.attr in model_names:
            out.append((anno.attr, card))
    elif isinstance(anno, ast.Constant) and isinstance(anno.value, str):
        # forward reference as a string: re-parse and recurse ("B", "B | None", …)
        try:
            inner = ast.parse(anno.value, mode="eval").body
        except SyntaxError:
            return
        _walk_anno(inner, model_names, card, out)
    elif isinstance(anno, ast.BinOp) and isinstance(anno.op, ast.BitOr):
        c = _looser(card, "0..1") if (_is_none(anno.left) or _is_none(anno.right)) else card
        for side in (anno.left, anno.right):
            if not _is_none(side):
                _walk_anno(side, model_names, c, out)
    elif isinstance(anno, ast.Subscript):
        head = _anno_head(anno.value)
        if head == "Literal":
            return  # values inside Literal[...] are not type references
        elts = _slice_elts(anno.slice)
        if head in _MANY_CTORS or head in _MAP_CTORS:
            child = _looser(card, "0..*")
            for e in elts:
                _walk_anno(e, model_names, child, out)
        elif head == "Optional":
            for e in elts:
                _walk_anno(e, model_names, _looser(card, "0..1"), out)
        elif head == "Union":
            c = _looser(card, "0..1") if any(_is_none(e) for e in elts) else card
            for e in elts:
                if not _is_none(e):
                    _walk_anno(e, model_names, c, out)
        else:  # Annotated / Final / ClassVar / … — recurse, preserve cardinality
            for e in elts:
                _walk_anno(e, model_names, card, out)


def _annotation_refs(anno: ast.expr, model_names: set[str]) -> list[tuple[str, str]]:
    """``(target_model, cardinality)`` for every model referenced in an annotation."""
    out: list[tuple[str, str]] = []
    _walk_anno(anno, model_names, "1", out)
    return out


def _associations(models: list[_Model]) -> list[_Assoc]:
    names = {m.name for m in models}
    seen: set[tuple[str, str, str]] = set()  # directional, per field label
    result: list[_Assoc] = []
    for m in models:
        for fname, anno in _fields(m.node):
            for target, card in _annotation_refs(anno, names):
                if target == m.name:
                    continue
                key = (m.name, target, fname)
                if key in seen:
                    continue
                seen.add(key)
                result.append(_Assoc(m.name, card, target, fname))
    return sorted(result, key=lambda a: (a.src, a.dst, a.label))


def render_domain_model() -> str:
    arch = load_architecture()
    components: dict[str, list[str]] = arch["components"]
    models = _discover_models(domain_files())
    model_names = {m.name for m in models}
    comp_of = {m.name: (component_of(m.module, components) or "Other") for m in models}
    assocs = _associations(models)

    body = ["# Domain model", ""]
    for comp in sorted(set(comp_of.values())):
        members = [m for m in models if comp_of[m.name] == comp]
        member_names = {m.name for m in members}
        comp_assocs = [a for a in assocs if comp_of.get(a.src) == comp]

        body += [f"## {comp}", "", "```mermaid", "classDiagram"]
        for m in members:
            scalar = [
                (n, ast.unparse(a))
                for n, a in _fields(m.node)
                if not _annotation_refs(a, model_names)
            ]
            if scalar:
                body.append(f"  class {m.name} {{")
                for n, t in scalar:
                    body.append(f"    +{n} {t}")
                body.append("  }")
            else:
                body.append(f"  class {m.name}")
        # stubs for association targets that live in another component
        for dst in sorted({a.dst for a in comp_assocs} - member_names):
            body.append(f"  class {dst}")
            body.append(f"  <<{comp_of.get(dst, 'Other')}>> {dst}")
        for a in comp_assocs:
            body.append(f'  {a.src} "1" --> "{a.card}" {a.dst} : {a.label}')
        body += ["```", ""]

    return with_header("\n".join(body).rstrip("\n") + "\n")
