"""Regenerate the data-store view and keep it anchored to StorageConfig.

The store topology is declared in ``[containers]``, but every store is anchored
to a real ``StorageConfig`` property. These tests fail if the declaration drifts
from the code in either direction — a store added to config without being
documented, or a documented store whose property was renamed/removed.
"""

from __future__ import annotations

import ast

from tests.guardrail import _containers as ct
from tests.guardrail._common import SRC_ROOT, load_architecture, write


def _storage_path_properties() -> set[str]:
    """@property names on StorageConfig that name a path/db store."""
    tree = ast.parse((SRC_ROOT / "config.py").read_text(encoding="utf-8"))
    cls = next(
        n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "StorageConfig"
    )
    props: set[str] = set()
    for node in cls.body:
        if (
            isinstance(node, ast.FunctionDef)
            and node.name.endswith(("_path", "_db_path"))
            and any(isinstance(d, ast.Name) and d.id == "property" for d in node.decorator_list)
        ):
            props.add(node.name)
    return props


def test_generate_container_diagram() -> None:
    out = write("containers.md", ct.render_container_diagram())
    assert out.exists()
    assert "graph LR" in out.read_text(encoding="utf-8")


def test_declared_stores_match_config() -> None:
    stores = ct.stores()
    cfg = load_architecture().get("containers", {})
    declared = {s["config_property"] for s in stores} | set(cfg.get("ignore_properties", []))
    actual = _storage_path_properties()

    undocumented = sorted(actual - declared)
    stale = sorted({s["config_property"] for s in stores} - actual)
    assert not undocumented, (
        "StorageConfig path properties missing from docs/architecture.toml "
        f"[containers] (add a store or ignore_properties): {undocumented}"
    )
    assert not stale, (
        f"[containers] stores reference StorageConfig properties that no longer exist: {stale}"
    )


def test_store_metadata_is_valid() -> None:
    components = set(load_architecture()["components"])
    stores = ct.stores()

    ids = [s["id"] for s in stores]
    assert len(ids) == len(set(ids)), "duplicate store ids"

    bad_owner = sorted({s["owner"] for s in stores if s["owner"] not in components})
    assert not bad_owner, f"store owners are not components: {bad_owner}"

    bad_role = sorted({s["role"] for s in stores if s["role"] not in ct.KNOWN_ROLES})
    assert not bad_role, f"unknown store roles (known: {sorted(ct.KNOWN_ROLES)}): {bad_role}"

    ref = sorted(
        {
            s["derived_from"]
            for s in stores
            if s.get("derived_from") and s["derived_from"] not in set(ids)
        }
    )
    assert not ref, f"derived_from references unknown store ids: {ref}"
