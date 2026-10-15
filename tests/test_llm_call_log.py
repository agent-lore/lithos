"""Replayable LLM call log (eval suite E1): blobs, store rows, writer, sweep, export, report."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lithos.config import LithosConfig
from lithos.lcma.call_log import (
    BLOB_SUBDIR,
    BlobStore,
    CallLogWriter,
    LlmCall,
    encode_request,
)
from lithos.lcma.edge_inference import (
    ChatMessage,
    NeighbourInfo,
    build_adjudication_prompt,
    request_payload,
)
from lithos.lcma.eval.export import case_from_call, export_cases
from lithos.lcma.eval.report import format_report, summarize_calls
from lithos.lcma.stats import StatsStore


def _neighbour(node_id: str, similarity: float = 0.6) -> NeighbourInfo:
    return NeighbourInfo(
        node_id=node_id,
        title=f"Note {node_id}",
        snippet="candidate text " * 80,
        similarity=similarity,
        namespace="default",
        entities=frozenset(),
        tags=frozenset(),
    )


def _payload(focus: str = "focus", *, snippet_chars: int = 100) -> dict[str, Any]:
    candidates = [_neighbour("c1"), _neighbour("c2", 0.5)]
    messages = build_adjudication_prompt(
        "Focus title", "focus content " * 50, candidates, snippet_chars=snippet_chars
    )
    return request_payload(
        messages,
        focus_id=focus,
        focus_title="Focus title",
        focus_content="focus content " * 50,
        candidates=candidates,
        snippet_chars=snippet_chars,
    )


def _call(**overrides: Any) -> LlmCall:
    base: dict[str, Any] = {
        "node_id": "focus",
        "doc_version": 1,
        "inference_version": 1,
        "model": "openai/gpt-5-mini",
        "reasoning_effort": None,
        "request": _payload(),
        "completion": json.dumps(
            {
                "judgements": [
                    {
                        "candidate": 1,
                        "relation": "supports",
                        "direction": "focus_to_candidate",
                        "confidence": 0.8,
                        "rationale": "evidence",
                    },
                    {"candidate": 2, "relation": "none"},
                ]
            }
        ),
        "outcome": "ok",
        "prompt_tokens": 300,
        "completion_tokens": 60,
        "reasoning_tokens": 20,
        "estimated": False,
        "latency_ms": 1200.0,
        "edges_written": 1,
    }
    base.update(overrides)
    return LlmCall(**base)


@pytest.fixture
async def store(test_config: LithosConfig):
    s = StatsStore(test_config)
    await s.open()
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
def blob_root(test_config: LithosConfig) -> Path:
    return test_config.storage.lithos_store_path / BLOB_SUBDIR


# ---------------------------------------------------------------------------
# BlobStore
# ---------------------------------------------------------------------------


class TestBlobStore:
    def test_put_is_content_addressed_and_deduped(self, tmp_path: Path) -> None:
        blobs = BlobStore(tmp_path / "blobs")
        sha = blobs.put(b"hello")
        assert sha == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        assert blobs.put(b"hello") == sha
        assert sorted(p.name for p in (tmp_path / "blobs").iterdir()) == [sha]
        assert blobs.get(sha) == b"hello"

    def test_get_missing_is_none(self, tmp_path: Path) -> None:
        assert BlobStore(tmp_path / "nowhere").get("0" * 64) is None

    def test_sweep_removes_only_unreferenced_digests(self, tmp_path: Path) -> None:
        blobs = BlobStore(tmp_path)
        keep = blobs.put(b"keep")
        drop = blobs.put(b"drop")
        (tmp_path / ".tmp-leftover").write_bytes(b"partial")
        (tmp_path / "notes.txt").write_text("not a blob")
        assert blobs.sweep({keep}) == 1
        assert blobs.get(keep) == b"keep"
        assert blobs.get(drop) is None
        assert (tmp_path / "notes.txt").exists()

    def test_sweep_and_usage_on_missing_root(self, tmp_path: Path) -> None:
        blobs = BlobStore(tmp_path / "absent")
        assert blobs.sweep(set()) == 0
        assert blobs.usage() == (0, 0)

    def test_usage_counts_blobs_only(self, tmp_path: Path) -> None:
        blobs = BlobStore(tmp_path)
        blobs.put(b"abc")
        blobs.put(b"defgh")
        (tmp_path / "notes.txt").write_text("x")
        assert blobs.usage() == (2, 8)

    def test_encode_request_is_canonical(self) -> None:
        assert encode_request({"b": 1, "a": "é"}) == encode_request({"a": "é", "b": 1})
        assert "é".encode() in encode_request({"a": "é"})


# ---------------------------------------------------------------------------
# StatsStore rows
# ---------------------------------------------------------------------------


class TestStoreRows:
    async def test_record_and_list_round_trip(self, store: StatsStore) -> None:
        row_id = await store.record_llm_call(
            _call().as_row(request_sha="r" * 64, completion_sha="c" * 64)
        )
        rows = await store.list_llm_calls()
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == row_id
        assert row["model"] == "openai/gpt-5-mini"
        assert row["reasoning_effort"] is None
        assert row["request_sha"] == "r" * 64
        assert row["completion_sha"] == "c" * 64
        assert json.loads(str(row["candidates_json"])) == [
            {"node_id": "c1", "similarity": 0.6},
            {"node_id": "c2", "similarity": 0.5},
        ]
        assert (row["prompt_tokens"], row["completion_tokens"], row["reasoning_tokens"]) == (
            300,
            60,
            20,
        )
        assert row["outcome"] == "ok"
        assert row["edges_written"] == 1
        assert row["shadow"] == 0
        assert datetime.fromisoformat(str(row["called_at"])).tzinfo is not None

    async def test_list_filters(self, store: StatsStore) -> None:
        await store.record_llm_call(_call().as_row(request_sha="a" * 64, completion_sha=None))
        await store.record_llm_call(
            _call(model="openai/gpt-6-luna", outcome="error", completion=None, error="boom").as_row(
                request_sha="b" * 64,
                completion_sha=None,
            )
        )
        await store.record_llm_call(
            _call(shadow=True).as_row(request_sha="c" * 64, completion_sha="d" * 64)
        )
        assert len(await store.list_llm_calls()) == 2  # live only by default
        assert len(await store.list_llm_calls(shadow=None)) == 3
        assert len(await store.list_llm_calls(shadow=True)) == 1
        assert [r["model"] for r in await store.list_llm_calls(model="openai/gpt-6-luna")] == [
            "openai/gpt-6-luna"
        ]
        assert [r["outcome"] for r in await store.list_llm_calls(outcomes={"ok"})] == ["ok"]
        assert await store.list_llm_calls(outcomes=set()) == []
        assert len(await store.list_llm_calls(limit=1)) == 1
        future = datetime.now(UTC) + timedelta(minutes=1)
        assert await store.list_llm_calls(since=future) == []
        past = datetime.now(UTC) - timedelta(minutes=1)
        assert len(await store.list_llm_calls(since=past)) == 2

    async def test_delete_before_and_referenced_shas(self, store: StatsStore) -> None:
        await store.record_llm_call(_call().as_row(request_sha="a" * 64, completion_sha="b" * 64))
        await store.record_llm_call(
            _call(outcome="error", completion=None).as_row(
                request_sha="c" * 64, completion_sha=None
            )
        )
        assert await store.referenced_llm_blob_shas() == {"a" * 64, "b" * 64, "c" * 64}
        assert await store.delete_llm_calls_before(datetime.now(UTC) - timedelta(days=1)) == 0
        assert await store.delete_llm_calls_before(datetime.now(UTC) + timedelta(seconds=1)) == 2
        assert await store.referenced_llm_blob_shas() == set()

    def test_unknown_outcome_rejected(self) -> None:
        with pytest.raises(ValueError, match="outcome"):
            _call(outcome="weird")


# ---------------------------------------------------------------------------
# CallLogWriter
# ---------------------------------------------------------------------------


class TestCallLogWriter:
    async def test_record_writes_two_blobs_and_a_row(
        self, store: StatsStore, blob_root: Path
    ) -> None:
        writer = CallLogWriter(store, blob_root)
        call = _call()
        await writer.record(call)
        rows = await store.list_llm_calls()
        assert len(rows) == 1
        request = writer.blobs.get(str(rows[0]["request_sha"]))
        completion = writer.blobs.get(str(rows[0]["completion_sha"]))
        assert request == encode_request(call.request)
        assert completion == (call.completion or "").encode()

    async def test_error_call_has_no_completion_blob(
        self, store: StatsStore, blob_root: Path
    ) -> None:
        writer = CallLogWriter(store, blob_root)
        await writer.record(_call(outcome="error", completion=None, error="timeout"))
        row = (await store.list_llm_calls())[0]
        assert row["completion_sha"] is None
        assert row["error"] == "timeout"
        assert writer.blobs.usage()[0] == 1

    async def test_record_failure_is_swallowed(self, store: StatsStore, blob_root: Path) -> None:
        writer = CallLogWriter(store, blob_root)
        store.record_llm_call = AsyncMock(side_effect=RuntimeError("disk full"))  # type: ignore[method-assign]
        await writer.record(_call())  # must not raise

    async def test_sweep_drops_old_rows_and_orphaned_blobs(
        self, store: StatsStore, blob_root: Path
    ) -> None:
        writer = CallLogWriter(store, blob_root)
        shared = _call()
        await writer.record(shared)  # same request+completion as the old row below
        await writer.record(_call(request=_payload("other"), completion='{"judgements": []}'))
        # Age the second row past the window, keep the first.
        rows = await store.list_llm_calls()
        old_id = min(int(r["id"]) for r in rows)
        import aiosqlite

        async with aiosqlite.connect(store.db_path) as db:
            await db.execute(
                "UPDATE llm_call_log SET called_at = ? WHERE id = ?",
                ((datetime.now(UTC) - timedelta(days=40)).isoformat(), old_id),
            )
            await db.commit()
        before_count, _ = writer.blobs.usage()
        assert before_count == 4
        rows_deleted, blobs_deleted = await writer.sweep(30)
        assert (rows_deleted, blobs_deleted) == (1, 2)
        remaining = await store.list_llm_calls()
        assert len(remaining) == 1
        assert writer.blobs.get(str(remaining[0]["request_sha"])) is not None
        assert writer.blobs.get(str(remaining[0]["completion_sha"])) is not None


# ---------------------------------------------------------------------------
# Export → cases
# ---------------------------------------------------------------------------


class TestExport:
    async def test_case_replays_the_exact_prompt(self, store: StatsStore, blob_root: Path) -> None:
        writer = CallLogWriter(store, blob_root)
        call = _call()
        await writer.record(call)
        cases, missing = await export_cases(store, writer.blobs, since=None)
        assert missing == 0
        assert len(cases) == 1
        case = cases[0]
        assert case["case_id"] == str((await store.list_llm_calls())[0]["request_sha"])[:16]
        assert case["messages"] == call.request["messages"]
        # Rebuilding the prompt from the structured inputs reproduces the messages byte-for-byte.
        rebuilt = build_adjudication_prompt(
            case["focus"]["title"],
            case["focus"]["content"],
            [
                NeighbourInfo(
                    node_id=c["node_id"],
                    title=c["title"],
                    snippet=c["snippet"],
                    similarity=c["similarity"],
                    namespace=c["namespace"],
                    entities=frozenset(),
                    tags=frozenset(),
                )
                for c in case["candidates"]
            ],
            snippet_chars=case["snippet_chars"],
        )
        assert [{"role": m.role, "content": m.content} for m in rebuilt] == case["messages"]
        assert isinstance(rebuilt[0], ChatMessage)
        ref = case["reference"]
        assert ref["model"] == "openai/gpt-5-mini"
        assert ref["completion"] == call.completion
        assert ref["problems"] == 0
        assert ref["judgements"] == [
            {
                "candidate_index": 1,
                "relation": "supports",
                "direction": "focus_to_candidate",
                "confidence": 0.8,
                "rationale": "evidence",
            }
        ]

    async def test_export_dedupes_on_request_and_skips_errors(
        self, store: StatsStore, blob_root: Path
    ) -> None:
        writer = CallLogWriter(store, blob_root)
        await writer.record(_call(completion='{"judgements": []}'))
        await writer.record(_call())  # same request, later → wins
        await writer.record(_call(request=_payload("x"), outcome="error", completion=None))
        cases, missing = await export_cases(store, writer.blobs, since=None)
        assert missing == 0
        assert len(cases) == 1
        assert cases[0]["reference"]["problems"] == 0
        assert cases[0]["reference"]["judgements"] is not None

    async def test_missing_blob_is_counted_not_fatal(
        self, store: StatsStore, blob_root: Path
    ) -> None:
        writer = CallLogWriter(store, blob_root)
        await writer.record(_call())
        writer.blobs.sweep(set())  # wipe every blob
        cases, missing = await export_cases(store, writer.blobs, since=None)
        assert (cases, missing) == ([], 1)

    async def test_limit_bounds_cases(self, store: StatsStore, blob_root: Path) -> None:
        writer = CallLogWriter(store, blob_root)
        for i in range(3):
            await writer.record(_call(request=_payload(f"n{i}")))
        cases, _ = await export_cases(store, writer.blobs, since=None, limit=2)
        assert len(cases) == 2

    def test_unparseable_reference_keeps_the_case(self) -> None:
        row = {
            "id": 1,
            "node_id": "focus",
            "doc_version": 1,
            "inference_version": 1,
            "called_at": "2026-10-15T00:00:00+00:00",
            "request_sha": "f" * 64,
            "model": "m",
            "reasoning_effort": "none",
            "outcome": "partial",
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "reasoning_tokens": None,
            "latency_ms": 5.0,
        }
        case = case_from_call(row, _payload(), "not json")
        assert case["reference"]["judgements"] is None
        assert case["reference"]["problems"] is None
        assert case["reference"]["reasoning_effort"] == "none"


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _row(**overrides: Any) -> dict[str, object]:
    base: dict[str, object] = {
        "model": "openai/gpt-5-mini",
        "reasoning_effort": None,
        "outcome": "ok",
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "reasoning_tokens": 100,
        "estimated": 0,
        "latency_ms": 1000.0,
        "edges_written": 2,
        "called_at": "2026-10-15T10:00:00+00:00",
    }
    base.update(overrides)
    return base


class TestReport:
    def test_groups_rates_and_token_means(self) -> None:
        rows = [
            _row(),
            _row(outcome="partial", edges_written=0, latency_ms=3000.0),
            _row(outcome="error", prompt_tokens=0, completion_tokens=0, reasoning_tokens=None),
            _row(
                model="openai/gpt-6-luna",
                reasoning_effort="none",
                reasoning_tokens=0,
                completion_tokens=100,
            ),
        ]
        summaries = summarize_calls(rows)
        assert [s["model"] for s in summaries] == ["openai/gpt-5-mini", "openai/gpt-6-luna"]
        mini = summaries[0]
        assert mini["calls"] == 3
        assert mini["outcomes"]["ok"] == 1 and mini["outcomes"]["error"] == 1
        assert mini["rates"]["error"] == pytest.approx(1 / 3)
        # token means exclude the errored call
        assert mini["prompt_tokens_mean"] == 1000
        assert mini["completion_tokens_mean"] == 200
        assert mini["reasoning_share"] == pytest.approx(0.5)
        assert mini["latency_ms_p50"] == 1000.0
        assert mini["latency_ms_p95"] == 3000.0
        assert mini["edges_per_call"] == pytest.approx(4 / 3)
        luna = summaries[1]
        assert luna["reasoning_effort"] == "none"
        assert luna["reasoning_share"] == 0.0

    def test_reasoning_share_none_when_unreported(self) -> None:
        [s] = summarize_calls([_row(reasoning_tokens=None)])
        assert s["reasoning_share"] is None

    def test_format_report_text(self) -> None:
        text = format_report(summarize_calls([_row(), _row(model="m2", reasoning_effort="low")]))
        assert "openai/gpt-5-mini (reasoning_effort=default)" in text
        assert "m2 (reasoning_effort=low)" in text
        assert "ok=100.0%" in text
        assert format_report([]) == "No LLM calls in the window."
