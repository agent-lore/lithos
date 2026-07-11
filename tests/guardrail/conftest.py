"""Session bootstrap: write every registered artifact before any test runs.

pytest collects files alphabetically, so on a fresh checkout the validation
tests (index links, manifest closure) would otherwise run before the
generators that come later in the ordering — making the very first
``make diagrams`` fail on missing artifacts. Generating everything up front
removes that ordering dependency: one run succeeds from an empty
``docs/generated/``. The driver tests then re-render (byte-identical — the
determinism the drift gate relies on) and keep their assertions.
"""

from __future__ import annotations

import pytest

from tests.guardrail import _component_pages, _diagram_toolkit, _index, _metrics_render
from tests.guardrail import _metrics_toolkit as mt
from tests.guardrail._common import LANGUAGE, load_architecture, write


def _generate_all() -> None:
    arch = load_architecture()
    write("architecture.md", _diagram_toolkit.render_component_diagram())
    if LANGUAGE == "python":
        write("domain_model.md", _diagram_toolkit.render_domain_model())
    if arch.get("containers", {}).get("stores"):
        # The module only ships with the containers adapter (short import line
        # so 88-width reflow can't strand the ignore comment).
        from tests.guardrail import _containers  # pyright: ignore

        write("containers.md", _containers.render_container_diagram())
    if arch.get("tool_catalog", {}).get("include_modules"):
        # Ditto: present only with the tool-catalog adapter.
        from tests.guardrail import _tool_catalog  # pyright: ignore

        write("tool_catalog.md", _tool_catalog.render_tool_catalog())
    metrics = mt.compute_metrics()
    write("metrics.json", _metrics_render.render_metrics_json(metrics))
    write("metrics.md", _metrics_render.render_metrics_md(metrics, arch.get("budgets", {})))
    for relpath, content in _component_pages.render_all().items():
        write(relpath, content)
    write("README.md", _index.render_index())


@pytest.fixture(scope="session", autouse=True)
def generated_docs() -> None:
    _generate_all()
