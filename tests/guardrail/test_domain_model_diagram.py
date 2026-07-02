"""Regenerate the domain class diagram from the code (drift-checked in CI).

This test rewrites ``docs/generated/domain_model.md`` from the dataclasses and
Pydantic models declared in the modules listed under ``[domain]`` in
``docs/architecture.toml``. CI runs this and fails if the committed file
changed — i.e. the diagram fell behind the code. Locally, run ``make diagrams``
and commit the result.
"""

from __future__ import annotations

from tests.guardrail import _diagram_toolkit as dt


def test_generate_domain_model_diagram() -> None:
    out = dt.write("domain_model.md", dt.render_domain_model())
    assert out.exists()
    assert "classDiagram" in out.read_text(encoding="utf-8")
