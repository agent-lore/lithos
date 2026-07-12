"""Cross-module private-symbol access scan (the ``seams`` metrics).

Two seam-hygiene rules, mechanized:

* src: module A reaching for module B's ``_private`` names — via direct import
  (``from b import _x``), module-attribute access (``b._x``), or attribute
  access on a value whose annotation names a class imported from B
  (``server._emit`` where ``server: LithosServer``, including
  ``TYPE_CHECKING``-only imports and string forward references);
* tests: the test suite importing ``_private`` names from the source package —
  such tests break on refactors that change no behaviour.

Both counts are optional ``[budgets]`` ratchets (``cross_module_private_refs``,
``tests_private_imports``). Python-only: cpp instances report a zeroed section
(underscore privacy is not a C++ convention). Dunders are never private here,
and same-module access never counts — the scan targets *cross-seam* reaches.
"""

from __future__ import annotations

import ast
import pathlib
from collections import Counter
from typing import Any

from tests.guardrail._common import LANGUAGE, REPO_ROOT, module_paths

DETAIL_CAP = 30


def _is_private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _dotted(expr: ast.expr) -> str | None:
    """``a.b.c`` for a pure Name/Attribute chain, else None."""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        base = _dotted(expr.value)
        return f"{base}.{expr.attr}" if base is not None else None
    return None


def _import_base(node: ast.ImportFrom, module: str | None, is_package: bool) -> str | None:
    """Absolute dotted base a ``from … import`` refers to (None if unresolvable)."""
    if node.level == 0:
        return node.module
    if module is None:
        return None  # relative import outside the scanned package (test files)
    parts = module.split(".")
    if not is_package:
        parts = parts[:-1]
    drop = node.level - 1
    if drop:
        if drop >= len(parts):
            return None
        parts = parts[: len(parts) - drop]
    base = ".".join(parts)
    return f"{base}.{node.module}" if node.module else base


def _chain_module(value: ast.expr, aliases: dict[str, str], internal: set[str]) -> str | None:
    """Internal module named by an expression chain, after alias substitution."""
    chain = _dotted(value)
    if chain is None:
        return None
    parts = chain.split(".")
    mapped = aliases.get(parts[0])
    if mapped is None:
        return None
    dotted = ".".join([mapped, *parts[1:]])
    return dotted if dotted in internal else None


def _anno_heads(anno: ast.expr) -> list[str]:
    """Class-name heads an annotation may resolve a value to.

    Containers (``list[X]`` …) are deliberately not descended: a value
    annotated ``list[X]`` is a list, and attribute access on it never reaches
    ``X``. Optional/Union/``|``/Annotated wrappers are transparent.
    """
    if isinstance(anno, ast.Name):
        return [anno.id]
    if isinstance(anno, ast.Attribute):
        chain = _dotted(anno)
        return [chain] if chain else []
    if isinstance(anno, ast.Constant) and isinstance(anno.value, str):
        try:
            inner = ast.parse(anno.value, mode="eval").body
        except SyntaxError:
            return []
        return _anno_heads(inner)
    if isinstance(anno, ast.BinOp) and isinstance(anno.op, ast.BitOr):
        return _anno_heads(anno.left) + _anno_heads(anno.right)
    if isinstance(anno, ast.Subscript):
        head = anno.value
        head_name = head.id if isinstance(head, ast.Name) else ""
        elts = list(anno.slice.elts) if isinstance(anno.slice, ast.Tuple) else [anno.slice]
        if head_name in {"Optional", "Union"}:
            return [h for e in elts for h in _anno_heads(e)]
        if head_name == "Annotated" and elts:
            return _anno_heads(elts[0])
    return []


def _resolve_head(
    head: str,
    symbol_imports: dict[str, str],
    aliases: dict[str, str],
    internal: set[str],
) -> tuple[str, str] | None:
    """Annotation head -> (source module, class name), when internally imported."""
    if "." not in head:
        source = symbol_imports.get(head)
        return (source, head) if source else None
    prefix, _, cls = head.rpartition(".")
    parts = prefix.split(".")
    mapped = aliases.get(parts[0])
    if mapped is None:
        return None
    dotted = ".".join([mapped, *parts[1:]])
    return (dotted, cls) if dotted in internal else None


def scan_tree(
    tree: ast.Module,
    internal: set[str],
    module: str | None = None,
    is_package: bool = False,
    annotations: bool = True,
) -> Counter[str]:
    """Private cross-module reach targets in one parsed source, with counts.

    ``internal`` is the full set of first-party module names; ``module`` the
    dotted name of the scanned source (None for test files — disables
    same-module exemption and relative-import resolution, both meaningless
    there). ``annotations=False`` turns off the annotated-instance level.
    """
    hits: Counter[str] = Counter()
    aliases: dict[str, str] = {}
    symbol_imports: dict[str, str] = {}

    # Pass 1 — bindings + import-level hits. ast.walk sees TYPE_CHECKING blocks.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in internal:
                    continue
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    # plain `import a.b` binds `a`; the chain resolver walks the rest
                    top = alias.name.split(".")[0]
                    aliases[top] = top
        elif isinstance(node, ast.ImportFrom):
            base = _import_base(node, module, is_package)
            if base is None or (base not in internal and base.rpartition(".")[0] not in internal):
                continue
            for alias in node.names:
                full = f"{base}.{alias.name}"
                if full in internal:
                    aliases[alias.asname or alias.name] = full
                    if _is_private(alias.name) and full != module:
                        hits[full] += 1
                elif base in internal:
                    if _is_private(alias.name):
                        if base != module:
                            hits[full] += 1
                    else:
                        symbol_imports[alias.asname or alias.name] = base

    bindings: dict[str, tuple[str, str]] = {}
    if annotations:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                args = node.args
                params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
                pairs = [(a.arg, a.annotation) for a in params if a.annotation]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                pairs = [(node.target.id, node.annotation)]
            else:
                continue
            for name, anno in pairs:
                resolved = {
                    r
                    for head in _anno_heads(anno)
                    if (r := _resolve_head(head, symbol_imports, aliases, internal))
                }
                if len({m for m, _ in resolved}) == 1:
                    bindings[name] = next(iter(resolved))

    # Pass 2 — attribute-level hits (module attrs, then annotated instances).
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and _is_private(node.attr)):
            continue
        target_module = _chain_module(node.value, aliases, internal)
        if target_module is not None:
            if target_module != module:
                hits[f"{target_module}.{node.attr}"] += 1
        elif isinstance(node.value, ast.Name) and node.value.id in bindings:
            source, cls = bindings[node.value.id]
            if source != module:
                hits[f"{source}.{cls}.{node.attr}"] += 1
    return hits


def _details(pairs: Counter[tuple[str, str]]) -> list[str]:
    entries = [
        (count, f"{importer} -> {target}" + (f" (x{count})" if count > 1 else ""))
        for (importer, target), count in pairs.items()
    ]
    return [text for _, text in sorted(entries, key=lambda e: (-e[0], e[1]))][:DETAIL_CAP]


def _test_files() -> list[pathlib.Path]:
    """Test sources scanned for private src imports.

    ``tests/guardrail/`` is excluded: the kit's own tooling legitimately
    imports its private ``tests.guardrail._*`` siblings.
    """
    tests_root = REPO_ROOT / "tests"
    return sorted(
        p for p in tests_root.rglob("*.py") if "guardrail" not in p.relative_to(tests_root).parts
    )


def seams_metrics() -> dict[str, Any]:
    zeroed = {
        "cross_module_private_refs": 0,
        "cross_module_private_detail": [],
        "tests_private_imports": 0,
        "tests_private_detail": [],
    }
    if LANGUAGE != "python":
        return zeroed

    internal = set(module_paths())
    src_pairs: Counter[tuple[str, str]] = Counter()
    for module, paths in sorted(module_paths().items()):
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            counts = scan_tree(tree, internal, module=module, is_package=path.name == "__init__.py")
            for target, n in counts.items():
                src_pairs[(module, target)] += n

    test_pairs: Counter[tuple[str, str]] = Counter()
    for path in _test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        counts = scan_tree(tree, internal, annotations=False)
        label = path.relative_to(REPO_ROOT).as_posix()
        for target, n in counts.items():
            test_pairs[(label, target)] += n

    return {
        "cross_module_private_refs": sum(src_pairs.values()),
        "cross_module_private_detail": _details(src_pairs),
        "tests_private_imports": sum(test_pairs.values()),
        "tests_private_detail": _details(test_pairs),
    }
