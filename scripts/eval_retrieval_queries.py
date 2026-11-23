#!/usr/bin/env python3
"""Build a retrieval-eval query set from a copy of ``stats.db`` receipts.

    python scripts/eval_retrieval_queries.py from-receipts COPY-OF-stats.db \\
        --agent ai-agents-briefing --agent codex-agent-foundations \\
        --since 2026-08-25 --per-agent 20 --slice ai-agents-briefing=agents \\
        --out queries.jsonl

One query per distinct query text per agent (case- and whitespace-folded),
latest receipt first, up to ``--per-agent``. Each row carries the caller's
``namespace_filter`` as its scope and the receipt id, timestamp and original
limit as its source, so the pool can be traced back to the call. Read a
**copy** of the database, never the live file.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from lithos.lcma.eval.retrieval.cases import write_jsonl


def _fold(text: str) -> str:
    return " ".join(text.lower().split())


def queries_from_receipts(
    db_path: Path,
    *,
    agents: list[str],
    since: str | None,
    per_agent: int,
    slices: dict[str, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        for agent in agents:
            params: list[Any] = [agent]
            where = "agent_id = ?"
            if since:
                where += " AND ts >= ?"
                params.append(since)
            seen: set[str] = set()
            cursor = db.execute(
                f'SELECT id, ts, query, "limit", namespace_filter FROM receipts '
                f"WHERE {where} ORDER BY ts DESC",
                params,
            )
            for receipt_id, ts, query, limit, namespace_filter in cursor:
                key = _fold(query)
                if not key or key in seen:
                    continue
                seen.add(key)
                scope: dict[str, Any] = {}
                if namespace_filter:
                    try:
                        parsed = json.loads(namespace_filter)
                    except json.JSONDecodeError:
                        parsed = [namespace_filter]
                    if parsed:
                        scope["namespace_filter"] = parsed
                rows.append(
                    {
                        "query_id": f"{agent}-{len(seen):03d}",
                        "query": query.strip(),
                        "caller": agent,
                        "slice": slices.get(agent, agent),
                        "scope": scope,
                        "source": {"receipt_id": receipt_id, "ts": ts, "limit": limit},
                    }
                )
                if len(seen) >= per_agent:
                    break
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("from-receipts", help="distinct recent queries per agent")
    rec.add_argument("stats_db", type=Path, help="a COPY of stats.db")
    rec.add_argument("--agent", action="append", required=True, dest="agents")
    rec.add_argument("--since", default=None, help="ISO date or datetime lower bound on ts")
    rec.add_argument("--per-agent", type=int, default=20)
    rec.add_argument(
        "--slice",
        action="append",
        default=[],
        dest="slices",
        help="agent=slice (default: the agent id)",
    )
    rec.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    slices = dict(item.split("=", 1) for item in args.slices)
    rows = queries_from_receipts(
        args.stats_db,
        agents=args.agents,
        since=args.since,
        per_agent=args.per_agent,
        slices=slices,
    )
    write_jsonl(args.out, rows)
    per = {agent: sum(1 for r in rows if r["caller"] == agent) for agent in args.agents}
    print(f"wrote {len(rows)} queries to {args.out}: {per}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
