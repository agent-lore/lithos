"""Eval runner (lithos.lcma.eval.runner): prompt replay, endpoint calls, run documents."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from lithos.config import LlmConfig
from lithos.lcma.edge_inference import INFERENCE_VERSION, build_adjudication_prompt
from lithos.lcma.eval.cases import case_from_dict, load_cases, load_labels
from lithos.lcma.eval.prices import ModelPrice
from lithos.lcma.eval.runner import (
    build_run_document,
    config_summary,
    load_run,
    messages_for,
    results_from_recorded,
    run_cases,
    run_slug,
    write_run,
)
from lithos.lcma.llm import LlmClient

SEED_CASES = Path("evals/edges/cases.jsonl")
SEED_LABELS = Path("evals/edges/labels.jsonl")


def _case(**overrides: Any):
    base = {
        "case_id": "c1",
        "focus": {"id": "f", "title": "Focus", "content": "x" * 900},
        "candidates": [
            {
                "index": 1,
                "node_id": "a",
                "title": "A",
                "snippet": "y" * 900,
                "similarity": 0.6,
                "namespace": "default",
            }
        ],
        "snippet_chars": 100,
    }
    base.update(overrides)
    return case_from_dict(base)


def _llm_config(**overrides: Any) -> LlmConfig:
    base: dict[str, Any] = {"base_url": "http://eval:1/v1", "model": "cand", "api_key": "k"}
    base.update(overrides)
    return LlmConfig(**base)


class TestMessages:
    def test_current_prompt_version_is_replayed_verbatim(self) -> None:
        case = _case(
            messages=[{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
            prompt_version=INFERENCE_VERSION,
        )
        assert [(m.role, m.content) for m in messages_for(case)] == [("system", "S"), ("user", "U")]

    def test_other_prompt_version_is_rebuilt(self) -> None:
        case = _case(
            messages=[{"role": "user", "content": "old"}], prompt_version=INFERENCE_VERSION + 1
        )
        rebuilt = messages_for(case)
        expected = build_adjudication_prompt(
            "Focus", "x" * 900, case.neighbours(), snippet_chars=100
        )
        assert rebuilt == expected
        assert "x" * 100 in rebuilt[1].content and "x" * 101 not in rebuilt[1].content


class TestRunCases:
    async def test_calls_endpoint_once_per_case_and_classifies(self) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            seen.append(body)
            content = {
                "judgements": [
                    {
                        "candidate": 1,
                        "relation": "supports",
                        "direction": "focus_to_candidate",
                        "confidence": 0.9,
                        "rationale": "r",
                    }
                ]
            }
            if "fail" in body["messages"][1]["content"]:
                return httpx.Response(500)
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": json.dumps(content)}}],
                    "usage": {
                        "prompt_tokens": 50,
                        "completion_tokens": 20,
                        "completion_tokens_details": {"reasoning_tokens": 5},
                    },
                },
            )

        cfg = _llm_config(reasoning_effort="none")
        client = LlmClient(cfg, transport=httpx.MockTransport(handler))
        cases = [
            _case(case_id="ok"),
            _case(case_id="bad", focus={"id": "f", "title": "fail", "content": "fail"}),
        ]
        seen_progress: list[str] = []
        results = await run_cases(
            cases,
            cfg,
            client=client,
            concurrency=2,
            progress=lambda r: seen_progress.append(r.case_id),
        )
        await client.close()
        assert len(seen) == 3  # ok once, bad twice (one retry on 5xx)
        assert seen[0]["model"] == "cand" and seen[0]["reasoning_effort"] == "none"
        by_id = {r.case_id: r for r in results}
        assert by_id["ok"].outcome == "ok"
        assert by_id["ok"].reasoning_tokens == 5
        assert by_id["ok"].latency_ms >= 0
        assert by_id["bad"].outcome == "error" and "500" in str(by_id["bad"].error)
        assert sorted(seen_progress) == ["bad", "ok"]

    async def test_owns_client_when_none_given(self) -> None:
        # Unreachable endpoint → every case errors, nothing raises, client is closed.
        cfg = _llm_config(base_url="http://127.0.0.1:9/v1", timeout_seconds=0.2)
        results = await run_cases([_case()], cfg)
        assert results[0].outcome == "error"


class TestRecordedAndDocuments:
    def test_results_from_recorded_scores_without_network(self) -> None:
        cases = load_cases(SEED_CASES)
        recorded = {
            json.loads(line)["case_id"]: json.loads(line)["completion"]
            for line in Path("evals/edges/recorded/seed-incumbent.jsonl").read_text().splitlines()
        }
        results = results_from_recorded(cases, recorded)
        assert len(results) == len(cases)
        assert all(r.estimated and r.latency_ms == 0.0 for r in results)
        assert {r.outcome for r in results} == {"ok"}
        assert results_from_recorded(cases, {}) == []

    def test_run_document_round_trip(self, tmp_path: Path) -> None:
        cases = load_cases(SEED_CASES)
        labels = load_labels(SEED_LABELS)
        recorded = {c.case_id: c.reference["completion"] for c in cases if c.reference}
        results = results_from_recorded(cases, recorded)
        cfg = _llm_config(model="openai/gpt-6-luna", reasoning_effort="low", temperature=0.3)
        doc = build_run_document(
            artefact="edges",
            cases=cases,
            results=results,
            llm_config=cfg,
            labels=labels,
            price=ModelPrice(0.1, 0.5),
            cases_path=SEED_CASES,
            labels_path=SEED_LABELS,
            recorded=True,
        )
        assert doc["run_id"].endswith("-openai_gpt-6-luna-low")
        assert doc["evidence"]["n_cases"] == len(cases)
        assert doc["evidence"]["result_case_ids"] == sorted(c.case_id for c in cases)
        assert len(doc["evidence"]["cases_fingerprint"]) == 64
        assert doc["evidence"]["labels_fingerprint"] is not None
        assert doc["config"]["temperature"] is None  # omitted while reasoning is on
        assert "api_key" not in json.dumps(doc)
        assert doc["metrics"]["labels"]["n_slots"] == len(labels)
        assert doc["metrics"]["cost_per_1k_calls"] is not None
        path = write_run(doc, tmp_path / "runs")
        assert path.name == f"{doc['run_id']}.json"
        assert load_run(path)["run_id"] == doc["run_id"]
        bad = tmp_path / "bad.json"
        bad.write_text("{}")
        with pytest.raises(ValueError, match="run document"):
            load_run(bad)

    def test_slug_and_config_summary(self) -> None:
        assert run_slug("openai/gpt-5-mini", None) == "openai_gpt-5-mini"
        assert run_slug("qwen/qwen3:30b", "none") == "qwen_qwen3_30b-none"
        summary = config_summary(_llm_config(temperature=0.2))
        assert summary["temperature"] == 0.2 and summary["prompt_version"] == INFERENCE_VERSION
