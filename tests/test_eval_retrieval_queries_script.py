"""scripts/eval_retrieval_queries.py — query set from a receipts copy."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from lithos.config import LithosConfig, StorageConfig
from lithos.lcma.stats import StatsStore

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import eval_retrieval_queries  # noqa: E402


async def _receipt(store: StatsStore, **over) -> None:
    base = dict(
        receipt_id=over.pop("receipt_id"),
        query=over.pop("query"),
        limit=over.pop("limit", 5),
        namespace_filter=over.pop("namespace_filter", None),
        scouts_fired=["scout_vector"],
        candidates_considered=3,
        final_nodes=[],
        conflicts_surfaced=[],
        surface_conflicts=False,
        temperature=0.5,
        terrace_reached=1,
        agent_id=over.pop("agent_id"),
        task_id=None,
    )
    await store.insert_receipt(**base)


@pytest.mark.asyncio
async def test_from_receipts_dedupes_and_scopes(tmp_path: Path) -> None:
    config = LithosConfig(storage=StorageConfig(data_dir=tmp_path))
    config.ensure_directories()
    store = StatsStore(config)
    await store.open()
    try:
        await _receipt(store, receipt_id="r1", query="HEMA events diary", agent_id="hema", limit=10)
        await _receipt(
            store, receipt_id="r2", query="hema  events DIARY", agent_id="hema", limit=10
        )
        await _receipt(
            store, receipt_id="r3", query="upcoming HEMA events", agent_id="hema", limit=3
        )
        await _receipt(
            store,
            receipt_id="r4",
            query="Morrow goals",
            agent_id="codex",
            namespace_filter=["research"],
        )
        await _receipt(store, receipt_id="r5", query="influx noise", agent_id="influx")
    finally:
        await store.close()

    out = tmp_path / "queries.jsonl"
    assert (
        eval_retrieval_queries.main(
            [
                "from-receipts",
                str(config.storage.stats_db_path),
                "--agent",
                "hema",
                "--agent",
                "codex",
                "--per-agent",
                "5",
                "--slice",
                "codex=agents",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["caller"] for r in rows] == ["hema", "hema", "codex"]  # influx not asked for
    hema = [r["query"] for r in rows if r["caller"] == "hema"]
    assert sorted(hema) == ["HEMA events diary", "upcoming HEMA events"]  # r2 folded into r1
    codex = next(r for r in rows if r["caller"] == "codex")
    assert codex["slice"] == "agents"
    assert codex["scope"] == {"namespace_filter": ["research"]}
    assert codex["source"]["receipt_id"] == "r4" and codex["source"]["limit"] == 5
    assert {r["query_id"] for r in rows} == {"hema-001", "hema-002", "codex-001"}

    out2 = tmp_path / "one.jsonl"
    eval_retrieval_queries.main(
        [
            "from-receipts",
            str(config.storage.stats_db_path),
            "--agent",
            "hema",
            "--per-agent",
            "1",
            "--out",
            str(out2),
        ]
    )
    assert len(out2.read_text().splitlines()) == 1
