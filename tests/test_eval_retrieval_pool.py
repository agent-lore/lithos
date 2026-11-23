"""Pooling a query set against a live component graph writes cases and no receipts."""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from lithos.cognitive_memory import pool_retrieval_eval
from lithos.lcma.eval.retrieval.cases import load_retrieval_cases, write_jsonl
from lithos.lcma.eval.retrieval.pool import vector_similarity
from lithos.server import LithosServer
from tests.helpers import call_tool

pytestmark = pytest.mark.integration


async def _seed(server: LithosServer) -> dict[str, str]:
    ids = {}
    for title, content in [
        ("Alpha algorithm performance", "Benchmarks of the alpha algorithm: faster than beta."),
        ("Beta rollout plan", "Rollout dates for beta: staging in October, production later."),
        ("Gamma summary", "Summary of gamma project progress and outcomes."),
    ]:
        result = await call_tool(
            server,
            "lithos_write",
            {"title": title, "content": content, "agent": "seed", "tags": ["eval-seed"]},
        )
        ids[title] = result["id"]
    return ids


async def _receipts(server: LithosServer) -> int:
    path = server.config.storage.stats_db_path
    if not path.exists():
        return 0
    async with aiosqlite.connect(path) as db:
        row = await (await db.execute("SELECT COUNT(*) FROM receipts")).fetchone()
        return int(row[0]) if row else 0


def test_vector_similarity_parses_the_reason_string() -> None:
    assert vector_similarity(["lexical match score 85.0", "semantic similarity 0.938"]) == 0.938
    assert vector_similarity(["coactivated 4 times with seeds"]) is None


@pytest.mark.asyncio
async def test_pool_writes_cases_and_leaves_no_receipt(
    server: LithosServer, tmp_path: Path
) -> None:
    ids = await _seed(server)
    queries = tmp_path / "queries.jsonl"
    write_jsonl(
        queries,
        [
            {"query_id": "q-alpha", "query": "alpha algorithm", "caller": "t", "slice": "seed"},
            {
                "query_id": "q-beta",
                "query": "beta rollout dates",
                "caller": "t",
                "slice": "seed",
                "scope": {"tags": ["eval-seed"]},
            },
            {
                "query_id": "q-empty-scope",
                "query": "anything",
                "caller": "t",
                "slice": "seed",
                "scope": {"tags": ["no-such-tag"]},
            },
        ],
    )
    out = tmp_path / "cases.jsonl"
    seen: list[str] = []
    n = await pool_retrieval_eval(
        memory=server.memory,
        search=server.search,
        knowledge=server.knowledge,
        queries_path=queries,
        out_path=out,
        data_dir=server.config.storage.data_dir,
        depth=5,
        text_chars=80,
        progress=seen.append,
    )
    assert n == 3 and seen == ["q-alpha", "q-beta", "q-empty-scope"]
    assert await _receipts(server) == 0

    cases = load_retrieval_cases([out])
    alpha = cases[0]
    assert set(alpha.methods) == {"hybrid", "semantic", "retrieve"}
    assert all(run.latency_ms > 0 for run in alpha.methods.values())
    assert alpha.methods["semantic"].ids[0] == ids["Alpha algorithm performance"]
    assert alpha.methods["hybrid"].ids[0] == ids["Alpha algorithm performance"]
    assert ids["Alpha algorithm performance"] in alpha.methods["retrieve"].ids
    top_retrieve = alpha.methods["retrieve"].results[0]
    assert top_retrieve.scouts  # recorded for the floor analysis
    assert alpha.snapshot["note_count"] == 3
    for note_id in alpha.pooled_ids():
        view = alpha.notes[note_id]
        assert view["title"] and view["path"] and len(view["text"]) <= 80

    empty = cases[2]
    assert all(run.ids == [] for run in empty.methods.values())

    raw = [json.loads(line) for line in out.read_text().splitlines()]
    assert raw[1]["scope"] == {"tags": ["eval-seed"]}
    assert raw[0]["methods"]["semantic"]["results"][0]["rank"] == 1
