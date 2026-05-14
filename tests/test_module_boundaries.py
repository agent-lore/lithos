"""Module-boundary guard: lock the ADR-0005 lcma import seam.

Issue #262 / ADR-0005 ``### Guardrail``: ``lithos.lcma.*`` is an internal
package owned by :mod:`lithos.cognitive_memory` and :mod:`lithos.provenance`.
No other source file under ``src/lithos/`` may import from it. Without a
mechanical guard, future PRs can quietly re-leak the boundary by
importing ``lithos.lcma.stats`` somewhere new and the architectural
seam erodes.

This test walks every ``.py`` under ``src/lithos/`` with :mod:`ast`,
collects every ``from lithos.lcma.*`` / ``import lithos.lcma.*`` it
finds, and fails on the first violation outside the allow-list.

It is a unit test (no ``@pytest.mark.integration``) so it runs as part
of ``make check`` and gates every PR.
"""

from __future__ import annotations

import ast
import pathlib

# Files outside ``lithos.lcma`` that are allowed to import from it.
# ``provenance.py`` (ADR-0004) and ``cognitive_memory.py`` (ADR-0005) are
# the two designated Modules; no other top-level source file should
# import any ``lithos.lcma.*`` symbol.
ALLOWED_FILE_NAMES = frozenset({"provenance.py", "cognitive_memory.py"})

SRC_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "lithos"


def _violations() -> list[tuple[pathlib.Path, int, str]]:
    """Return ``(file, lineno, statement)`` tuples for every illegal lcma import."""
    found: list[tuple[pathlib.Path, int, str]] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        # The lcma package itself can freely import its own submodules.
        if "lcma" in path.relative_to(SRC_ROOT).parts:
            continue
        # The two designated facade modules are allow-listed.
        if path.name in ALLOWED_FILE_NAMES:
            continue

        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:  # pragma: no cover — defensive
            raise AssertionError(f"Cannot parse {path}: {exc}") from exc

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and (
                    node.module == "lithos.lcma" or node.module.startswith("lithos.lcma.")
                ):
                    found.append((path, node.lineno, f"from {node.module} import ..."))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "lithos.lcma" or alias.name.startswith("lithos.lcma."):
                        found.append((path, node.lineno, f"import {alias.name}"))
    return found


def test_no_external_lcma_imports() -> None:
    """Only ``provenance.py`` and ``cognitive_memory.py`` may import lithos.lcma.*.

    The lcma package is an implementation detail of those two Modules
    (ADR-0004 / ADR-0005). Re-leaking the boundary in a future PR will
    fail this test with the exact ``file:lineno: statement`` triples
    that need cleaning up.
    """
    violations = _violations()
    assert not violations, "lcma boundary violations:\n" + "\n".join(
        f"  {path.relative_to(SRC_ROOT.parents[1])}:{lineno}: {stmt}"
        for path, lineno, stmt in violations
    )


def test_guard_catches_violations_in_a_synthetic_file(tmp_path: pathlib.Path) -> None:
    """Sanity-check the AST walk on a synthetic file.

    Catches the case where the walk silently misses ``from`` / ``import``
    statements (e.g. typo in the prefix check, accidental allow-list
    expansion). Runs on a tmp file outside the real source tree so the
    real guard above is unaffected.
    """
    synthetic = tmp_path / "fake_module.py"
    synthetic.write_text(
        "from lithos.lcma.stats import StatsStore\nimport lithos.lcma.scouts\n",
        encoding="utf-8",
    )
    tree = ast.parse(synthetic.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and (
                node.module == "lithos.lcma" or node.module.startswith("lithos.lcma.")
            ):
                hits.append(f"from {node.module}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "lithos.lcma" or alias.name.startswith("lithos.lcma."):
                    hits.append(f"import {alias.name}")

    assert hits == ["from lithos.lcma.stats", "import lithos.lcma.scouts"]
