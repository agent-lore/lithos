"""Enforce the directional architecture contract.

Python projects: the contracts live in ``pyproject.toml`` under
``[tool.importlinter]`` (dependencies must only point downward,
Entrypoints -> Core -> Foundation, expressed as ``forbidden`` contracts); this
test runs ``lint-imports`` and fails with its report if any contract is broken.

C++ projects (``[project] language = "cpp"``): the same downward-only rule is
asserted directly on the include graph — no component may depend on one in a
higher tier (tier order = declaration order in ``[tiers]``).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib

import pytest

from tests.guardrail import _diagram_toolkit as dt
from tests.guardrail._common import LANGUAGE, REPO_ROOT, load_architecture


def _assert_import_linter_contracts() -> None:
    exe = shutil.which("lint-imports")
    cmd = [exe] if exe else [sys.executable, "-m", "importlinter"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:  # pragma: no cover - import-linter must be installed
        pytest.skip("import-linter not installed")
    assert result.returncode == 0, (
        "import-linter architecture contracts broken:\n" + result.stdout + result.stderr
    )


def _assert_no_upward_tier_edges() -> None:
    arch = load_architecture()
    tiers: dict[str, list[str]] = arch.get("tiers", {})
    assert tiers, "docs/architecture.toml needs [tiers] to enforce the layering contract"
    rank = {comp: i for i, members in enumerate(tiers.values()) for comp in members}
    upward = sorted(
        f"{src} -> {dst}"
        for src, dst in dt.component_edges(arch["components"])
        if src in rank and dst in rank and rank[dst] < rank[src]
    )
    assert not upward, (
        "dependencies must only point downward through [tiers]; upward edges found:\n"
        + "\n".join(f"  {edge}" for edge in upward)
    )


def test_layering_contract_holds() -> None:
    if LANGUAGE == "cpp":
        _assert_no_upward_tier_edges()
    else:
        _assert_import_linter_contracts()


def _tier_prefixes(arch: dict) -> list[set[str]]:
    """Module-prefix sets per declared tier, in [tiers] declaration order."""
    components: dict[str, list[str]] = arch["components"]
    return [
        {prefix for comp in members if comp in components for prefix in components[comp]}
        for members in arch.get("tiers", {}).values()
    ]


def _covered(prefix: str, entries: list[str]) -> bool:
    """True if *prefix* falls under any of the contract's module *entries*."""
    return any(prefix == e or prefix.startswith(e + ".") for e in entries)


def test_importlinter_contracts_cover_architecture_tiers() -> None:
    """Every downward-only rule implied by [tiers] is enforced by some contract.

    [tiers]/[components] drive the diagram and metrics; import-linter enforces
    direction from hand-maintained module lists. This is a COVERAGE check: for
    each lower-tier module prefix and each higher-tier prefix, at least one
    forbidden contract must ban that import. Contracts may be stricter than
    the tier map (e.g. holding an individual Core module to Foundation
    discipline) — extra strictness is welcome; a gap is not. Stale contract
    entries are caught by import-linter itself (unknown module = error).
    """
    if LANGUAGE != "python":
        pytest.skip("cpp derives the contract from [tiers] directly — nothing to synchronize")
    arch = load_architecture()
    tiers = _tier_prefixes(arch)
    assert len(tiers) >= 2, "docs/architecture.toml needs at least two [tiers] to imply rules"

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = [
        c
        for c in pyproject.get("tool", {}).get("importlinter", {}).get("contracts", [])
        if c.get("type") == "forbidden"
    ]
    assert contracts, "pyproject.toml [tool.importlinter] has no forbidden contracts"

    gaps = [
        f"{src} may import {target}"
        for low in range(1, len(tiers))
        for src in sorted(tiers[low])
        for high in range(low)
        for target in sorted(tiers[high])
        if not any(
            _covered(src, c.get("source_modules", []))
            and _covered(target, c.get("forbidden_modules", []))
            for c in contracts
        )
    ]
    assert not gaps, (
        "[tiers] in docs/architecture.toml implies downward-only rules that no "
        "[tool.importlinter] forbidden contract enforces — add the missing "
        "source/forbidden entries (or fix the tier map):\n  " + "\n  ".join(gaps)
    )
