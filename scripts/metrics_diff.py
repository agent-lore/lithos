#!/usr/bin/env python3
"""Diff two architecture-metrics snapshots (docs/generated/metrics.json).

Prints one row per changed scalar metric, keyed by dotted path. Used by CI to
append an informational delta table to the pull-request step summary, and
locally via ``make metrics-diff``.

Usage:
    python scripts/metrics_diff.py BASE.json HEAD.json [--markdown]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections.abc import Sequence
from typing import Any

Scalar = int | float | str | bool | None


def _flatten(value: Any, prefix: str = "") -> dict[str, Scalar]:
    """Dotted-path -> scalar map; lists are summarized by length."""
    flat: dict[str, Scalar] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        flat[f"{prefix}.count"] = len(value)
    else:
        flat[prefix] = value
    return flat


def diff_metrics(old: dict, new: dict) -> list[tuple[str, Scalar, Scalar]]:
    """(dotted key, old value, new value) for every changed scalar, sorted."""
    old_flat, new_flat = _flatten(old), _flatten(new)
    keys = sorted(set(old_flat) | set(new_flat))
    return [
        (key, old_flat.get(key), new_flat.get(key))
        for key in keys
        if old_flat.get(key) != new_flat.get(key)
    ]


def _delta(old: Scalar, new: Scalar) -> str:
    if isinstance(old, int | float) and isinstance(new, int | float):
        change = new - old
        return f"{change:+g}"
    return ""


def render(changes: Sequence[tuple[str, Scalar, Scalar]], markdown: bool) -> str:
    if not changes:
        return "### Architecture metrics: no changes\n" if markdown else "No metric changes.\n"
    if markdown:
        lines = [
            "### Architecture metrics delta",
            "",
            "| Metric | Base | Head | Δ |",
            "|---|---:|---:|---:|",
        ]
        lines.extend(
            f"| `{key}` | {old if old is not None else '—'} |"
            f" {new if new is not None else '—'} | {_delta(old, new)} |"
            for key, old, new in changes
        )
        return "\n".join(lines) + "\n"
    width = max(len(key) for key, _, _ in changes)
    return (
        "\n".join(
            f"{key.ljust(width)}  {old!r:>10} -> {new!r} {_delta(old, new)}"
            for key, old, new in changes
        )
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=pathlib.Path)
    parser.add_argument("head", type=pathlib.Path)
    parser.add_argument("--markdown", action="store_true", help="emit a GitHub markdown table")
    args = parser.parse_args(argv)

    try:
        old = json.loads(args.base.read_text(encoding="utf-8"))
        new = json.loads(args.head.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"metrics_diff: cannot read snapshots: {exc}", file=sys.stderr)
        return 2

    # No base snapshot (e.g. the port's own first PR, before metrics.json exists
    # on the base branch): a full "— -> value" table for every metric is noise.
    if not old:
        sys.stdout.write(
            "### Architecture metrics: first snapshot (no base to diff)\n"
            if args.markdown
            else "First metrics snapshot — no base to diff.\n"
        )
        return 0

    sys.stdout.write(render(diff_metrics(old, new), markdown=args.markdown))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
