"""Regenerate the docs/generated/README.md index (drift-checked in CI).

The index is generated from the artifact registry in ``_index.py`` so it can
never drift from the set of artifacts actually produced.
"""

from __future__ import annotations

from tests.guardrail import _index
from tests.guardrail._common import GENERATED_DIR, write


def test_generate_index() -> None:
    out = write("README.md", _index.render_index())
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    for artifact in _index.artifacts():
        assert f"({artifact.path})" in text, f"index does not link {artifact.path}"


def test_index_links_resolve() -> None:
    for artifact in _index.artifacts():
        assert (GENERATED_DIR / artifact.path).exists(), (
            f"registered artifact {artifact.path} is missing from docs/generated/ — "
            "did its generator run?"
        )


def test_optional_adapters_drop_from_registry_when_unconfigured() -> None:
    """A ported project without [containers]/[tool_catalog] gets neither artifact."""
    minimal = {"components": {"A": ["pkg.a"]}}  # no containers, no tool_catalog
    paths = {a.path for a in _index.artifacts(minimal)}
    assert "containers.md" not in paths
    assert "tool_catalog.md" not in paths
    assert {"architecture.md", "domain_model.md", "metrics.md", "metrics.json"} <= paths


def test_usage_lines_only_link_present_artifacts() -> None:
    """The 'How to use' copy must not reference an adapter that wasn't generated."""
    without = "\n".join(_index._usage_lines({"architecture.md", "domain_model.md", "metrics.md"}))
    assert "containers.md" not in without
    assert "tool_catalog.md" not in without

    full = {"architecture.md", "domain_model.md", "containers.md", "tool_catalog.md", "metrics.md"}
    with_all = "\n".join(_index._usage_lines(full))
    assert "containers.md" in with_all
    assert "tool_catalog.md" in with_all
