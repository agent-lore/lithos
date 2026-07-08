"""Regenerate the domain class diagram from the code (drift-checked in CI).

This test rewrites ``docs/generated/domain_model.md`` from the dataclasses and
Pydantic models declared in the modules listed under ``[domain]`` in
``docs/architecture.toml``. CI runs this and fails if the committed file
changed — i.e. the diagram fell behind the code. Locally, run ``make diagrams``
and commit the result.

Also unit-tests the structure-aware association extraction, and the
completeness guard that every public model is classified in the config.
"""

from __future__ import annotations

import ast

import pytest

from tests.guardrail import _diagram_toolkit as dt


def test_generate_domain_model_diagram() -> None:
    out = dt.write("domain_model.md", dt.render_domain_model())
    assert out.exists()
    assert "classDiagram" in out.read_text(encoding="utf-8")


def test_every_model_is_scanned_or_excluded() -> None:
    """A public model in a module that is neither included nor excluded fails."""
    domain = dt.load_architecture().get("domain", {})
    include = domain.get("include_modules", [])
    exclude = domain.get("exclude_modules", [])

    assert not (set(include) & set(exclude)), "domain include/exclude overlap"

    offenders = sorted(
        {
            f"{m.module}.{m.name}"
            for m in dt.discover_all_models()
            if not (
                any(m.module == p or m.module.startswith(p + ".") for p in include)
                or dt._excluded(m.module, exclude)
            )
        }
    )
    assert not offenders, (
        "public models in modules neither included nor excluded in "
        "docs/architecture.toml [domain] (add the module to include_modules to "
        f"diagram them, or exclude_modules if they are not domain entities): {offenders}"
    )


def test_no_duplicate_model_names() -> None:
    """Same-named models in different modules would collapse into one diagram node."""
    dupes = dt.duplicate_model_names(dt.domain_files())
    assert not dupes, (
        "public model class names defined in more than one module would collapse "
        "in the domain diagram and share a Mermaid id — rename one, or exclude its "
        f"module in docs/architecture.toml [domain]: {dupes}"
    )


def _card(annotation: str, targets: set[str]) -> list[tuple[str, str]]:
    node = ast.parse(annotation, mode="eval").body
    return sorted(dt._annotation_refs(node, targets))


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        ("X", [("X", "1")]),
        ("X | None", [("X", "0..1")]),
        ("Optional[X]", [("X", "0..1")]),
        ("list[X]", [("X", "0..*")]),
        ("set[X]", [("X", "0..*")]),
        ("dict[str, X]", [("X", "0..*")]),
        ("Mapping[str, list[X]]", [("X", "0..*")]),
        ("list[X] | None", [("X", "0..*")]),  # many wins over optional
        ("Literal['X', 'y']", []),  # literal values are not type refs
        ("int", []),
        ("dict[str, int]", []),
    ],
)
def test_annotation_ref_cardinalities(annotation: str, expected: list[tuple[str, str]]) -> None:
    assert _card(annotation, {"X"}) == expected


def test_associations_are_directional_per_field() -> None:
    src = """
from dataclasses import dataclass

@dataclass
class A:
    to_b: "B"
    also_b: "B | None"

@dataclass
class B:
    back_to_a: "A"
"""
    tree = ast.parse(src)
    models = [dt._Model(node=n, module="m") for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assocs = dt._associations(models)
    # two distinct A->B edges (one per field) plus the B->A back-reference
    labels = {(a.src, a.dst, a.label, a.card) for a in assocs}
    assert ("A", "B", "to_b", "1") in labels
    assert ("A", "B", "also_b", "0..1") in labels
    assert ("B", "A", "back_to_a", "1") in labels
