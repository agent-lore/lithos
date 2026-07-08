"""The docs/generated/ directory must exactly match the artifact registry.

The CI git-diff gate catches *modified* artifacts, but not a generator that is
renamed or deleted while its old committed output lingers. This manifest check
makes stale or unregistered files a test failure with an actionable message.
"""

from __future__ import annotations

from tests.guardrail import _index


def test_generated_dir_matches_registry() -> None:
    expected = _index.all_expected_paths()
    actual = _index.generated_files()

    unregistered = sorted(actual - expected)
    missing = sorted(expected - actual)
    assert not unregistered and not missing, (
        "docs/generated/ disagrees with the artifact registry "
        "(tests/guardrail/_index.py).\n"
        + (
            "Files present but not registered (register or delete them):\n"
            + "\n".join(f"  {p}" for p in unregistered)
            + "\n"
            if unregistered
            else ""
        )
        + (
            "Registered but not generated (did a generator stop running?):\n"
            + "\n".join(f"  {p}" for p in missing)
            if missing
            else ""
        )
    )
