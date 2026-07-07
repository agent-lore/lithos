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
