"""Enforce the directional architecture contract via import-linter.

The contracts live in ``pyproject.toml`` under ``[tool.importlinter]``:
dependencies must only point downward (Entrypoints -> Core -> Foundation),
expressed as ``forbidden`` contracts. This test simply runs ``lint-imports``
and fails with its report if any contract is broken. The complementary
``lithos.lcma`` seam is guarded by ``tests/test_module_boundaries.py``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest


def test_import_contracts_hold() -> None:
    exe = shutil.which("lint-imports")
    cmd = [exe] if exe else [sys.executable, "-m", "importlinter"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:  # pragma: no cover - import-linter must be installed
        pytest.skip("import-linter not installed")
    assert result.returncode == 0, (
        "import-linter architecture contracts broken:\n" + result.stdout + result.stderr
    )
