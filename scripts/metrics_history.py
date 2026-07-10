#!/usr/bin/env python3
"""Mine the metric time series from the git history of metrics.json.

Because ``docs/generated/metrics.json`` is committed and regenerated
deterministically, its git history *is* the architecture-metrics time series —
no database needed. This walks first-parent history (one point per mainline
commit that touched the snapshot) and emits CSV or a Mermaid ``xychart-beta``
per metric.

Usage:
    python scripts/metrics_history.py [--format csv|mermaid] [--keys k1,k2,...]

Keys are dotted paths into metrics.json; a list-valued path is summarized by
appending ``.count`` (same convention as scripts/metrics_diff.py).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

SNAPSHOT = "docs/generated/metrics.json"

DEFAULT_KEYS = [
    "graph.cross_component_edges",
    "graph.component_cycles.count",
    "graph.module_cycles.count",
    "size.modules_over_800.count",
    "size.max_module_lines",
    "size.total_sloc",
    "complexity.functions_over_10",
]


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def iter_snapshots() -> list[tuple[str, str, dict]]:
    """(short sha, date, metrics dict) per first-parent commit touching the snapshot."""
    log = _git("log", "--reverse", "--first-parent", "--format=%h %cs", "--", SNAPSHOT).splitlines()
    snapshots: list[tuple[str, str, dict]] = []
    for line in log:
        sha, date = line.split(maxsplit=1)
        try:
            metrics = json.loads(_git("show", f"{sha}:{SNAPSHOT}"))
        except subprocess.CalledProcessError:
            continue  # commit removed the file (or predates it)
        except json.JSONDecodeError:
            continue  # snapshot at this commit is malformed; skip, don't crash the walk
        snapshots.append((sha, date, metrics))
    return snapshots


def extract(metrics: dict, dotted_key: str) -> Any:
    value: Any = metrics
    for part in dotted_key.split("."):
        if part == "count" and isinstance(value, list):
            return len(value)
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def emit_csv(snapshots: list[tuple[str, str, dict]], keys: list[str]) -> str:
    lines = ["sha,date," + ",".join(keys)]
    lines.extend(
        f"{sha},{date},"
        + ",".join(str(extract(m, k) if extract(m, k) is not None else "") for k in keys)
        for sha, date, m in snapshots
    )
    return "\n".join(lines) + "\n"


def emit_mermaid(snapshots: list[tuple[str, str, dict]], keys: list[str]) -> str:
    blocks: list[str] = []
    labels = ", ".join(f'"{sha}"' for sha, _, _ in snapshots)
    for key in keys:
        values = [extract(m, key) for _, _, m in snapshots]
        points = ", ".join("0" if v is None else str(v) for v in values)
        blocks.append(
            "\n".join(
                [
                    f"## {key}",
                    "",
                    "```mermaid",
                    "xychart-beta",
                    f'    title "{key}"',
                    f"    x-axis [{labels}]",
                    f"    line [{points}]",
                    "```",
                ]
            )
        )
    return "\n\n".join(blocks) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("csv", "mermaid"), default="csv")
    parser.add_argument(
        "--keys",
        default=",".join(DEFAULT_KEYS),
        help="comma-separated dotted paths into metrics.json",
    )
    args = parser.parse_args(argv)
    keys = [k.strip() for k in args.keys.split(",") if k.strip()]

    snapshots = iter_snapshots()
    if not snapshots:
        print(f"metrics_history: no history for {SNAPSHOT}", file=sys.stderr)
        return 1

    emit = emit_csv if args.format == "csv" else emit_mermaid
    sys.stdout.write(emit(snapshots, keys))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
