"""Regenerate the component dependency diagram from the real import graph.

Uses :mod:`grimp` to compute actual module imports, grouped into the components
declared in ``docs/architecture.toml``, and rewrites
``docs/generated/architecture.md``. CI fails if the committed diagram drifts
from the code — so an unexpected cross-component edge shows up as a reviewable
diff. The directional rules themselves are enforced separately by
``test_layering_contract.py`` (import-linter).
"""

from __future__ import annotations

from tests.guardrail import _diagram_toolkit as dt


def test_generate_component_diagram() -> None:
    out = dt.write("architecture.md", dt.render_component_diagram())
    assert out.exists()
    assert "graph TD" in out.read_text(encoding="utf-8")


def test_every_internal_module_maps_to_a_component() -> None:
    """No internal module should be missing from the component map (orphan check)."""
    arch = dt.load_architecture()
    components = arch["components"]
    graph = dt.build_import_graph()  # memoized in _common; don't rebuild
    orphans = sorted(
        m for m in graph.modules if m != dt.ROOT_PACKAGE and dt.component_of(m, components) is None
    )
    assert not orphans, (
        "Modules not mapped to any component in docs/architecture.toml:\n"
        + "\n".join(f"  {m}" for m in orphans)
    )
