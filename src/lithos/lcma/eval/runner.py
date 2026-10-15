"""Run a golden set against an OpenAI-compatible endpoint and record the result.

Reuses the production client and the production prompt builder/parser only;
nothing here touches a store. A run document (``evals/runs/<ts>-<slug>.json``)
carries the config that produced it (never the API key), one result per
case, and the E3 metrics table, so two runs are comparable later without
re-running either.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lithos.config import LlmConfig
from lithos.errors import LlmError
from lithos.lcma.edge_inference import INFERENCE_VERSION, build_adjudication_prompt
from lithos.lcma.eval.cases import Case, Label, Slot
from lithos.lcma.eval.prices import ModelPrice
from lithos.lcma.eval.scoring import CaseResult, classify_completion, compute_metrics
from lithos.lcma.llm import ChatMessage, LlmClient, LlmResult

_CHARS_PER_TOKEN = 4


def messages_for(case: Case) -> list[ChatMessage]:
    """Replay the verbatim prompt when it is the current prompt version, else rebuild.

    A case exported under an older ``INFERENCE_VERSION`` is re-rendered from
    its structured inputs so the run exercises the prompt that would ship.
    """
    if case.messages and case.prompt_version == INFERENCE_VERSION:
        return [ChatMessage(str(m["role"]), str(m["content"])) for m in case.messages]
    return build_adjudication_prompt(
        str(case.focus["title"]),
        str(case.focus["content"]),
        case.neighbours(),
        snippet_chars=case.snippet_chars,
    )


def result_from_llm(case: Case, result: LlmResult, latency_ms: float) -> CaseResult:
    outcome, problems, judgements = classify_completion(case, result.text)
    return CaseResult(
        case_id=case.case_id,
        outcome=outcome,
        problems=problems,
        judgements=judgements,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        reasoning_tokens=result.reasoning_tokens,
        estimated=result.estimated,
        latency_ms=latency_ms,
        completion=result.text,
    )


def results_from_recorded(cases: list[Case], recorded: dict[str, str]) -> list[CaseResult]:
    """Score recorded completions with no network (the ``eval-smoke`` path).

    Tokens are the chars//4 estimate and latency is zero: this path exists to
    gate parser and scorer changes, not to measure a model.
    """
    results: list[CaseResult] = []
    for case in cases:
        completion = recorded.get(case.case_id)
        if completion is None:
            continue
        outcome, problems, judgements = classify_completion(case, completion)
        prompt_chars = sum(len(m.content) for m in messages_for(case))
        results.append(
            CaseResult(
                case_id=case.case_id,
                outcome=outcome,
                problems=problems,
                judgements=judgements,
                prompt_tokens=prompt_chars // _CHARS_PER_TOKEN,
                completion_tokens=len(completion) // _CHARS_PER_TOKEN,
                reasoning_tokens=None,
                estimated=True,
                latency_ms=0.0,
                completion=completion,
            )
        )
    return results


async def run_cases(
    cases: list[Case],
    llm_config: LlmConfig,
    *,
    concurrency: int = 4,
    client: LlmClient | None = None,
    progress: Callable[[CaseResult], None] | None = None,
) -> list[CaseResult]:
    """Call the endpoint once per case (bounded concurrency); never raises per case."""
    own_client = client is None
    llm = client or LlmClient(llm_config)
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def one(case: Case) -> CaseResult:
        messages = messages_for(case)
        async with semaphore:
            started = time.monotonic()
            try:
                result = await llm.chat(messages)
            except LlmError as exc:
                outcome = CaseResult(
                    case_id=case.case_id,
                    outcome="error",
                    problems=case.n_candidates,
                    judgements=None,
                    prompt_tokens=0,
                    completion_tokens=0,
                    reasoning_tokens=None,
                    estimated=False,
                    latency_ms=(time.monotonic() - started) * 1000.0,
                    completion=None,
                    error=str(exc),
                )
            else:
                outcome = result_from_llm(case, result, (time.monotonic() - started) * 1000.0)
        if progress is not None:
            progress(outcome)
        return outcome

    try:
        return list(await asyncio.gather(*(one(case) for case in cases)))
    finally:
        if own_client:
            await llm.close()


def run_slug(model: str, reasoning_effort: str | None) -> str:
    slug = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in model)
    return f"{slug}-{reasoning_effort}" if reasoning_effort else slug


def config_summary(llm_config: LlmConfig) -> dict[str, Any]:
    """The knobs that shaped the run — everything but the credential."""
    return {
        "model": llm_config.model,
        "base_url": llm_config.base_url,
        "reasoning_effort": llm_config.reasoning_effort,
        "temperature": None if llm_config.reasoning_on else llm_config.temperature,
        "max_output_tokens": llm_config.max_output_tokens,
        "extra_body": llm_config.extra_body,
        "confidence_floor": llm_config.confidence_floor,
        "snippet_chars": llm_config.snippet_chars,
        "prompt_version": INFERENCE_VERSION,
    }


def build_run_document(
    *,
    artefact: str,
    cases: list[Case],
    results: list[CaseResult],
    llm_config: LlmConfig,
    labels: dict[Slot, Label] | None,
    price: ModelPrice | None,
    cases_path: Path | None,
    labels_path: Path | None,
    recorded: bool = False,
) -> dict[str, Any]:
    metrics = compute_metrics(
        cases, results, labels=labels, confidence_floor=llm_config.confidence_floor, price=price
    )
    return {
        "run_id": f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{run_slug(llm_config.model, llm_config.reasoning_effort)}",
        "created_at": datetime.now(UTC).isoformat(),
        "artefact": artefact,
        "recorded": recorded,
        "cases_path": str(cases_path) if cases_path else None,
        "labels_path": str(labels_path) if labels_path else None,
        "config": config_summary(llm_config),
        "metrics": metrics,
        "results": [r.__dict__ for r in results],
    }


def write_run(document: dict[str, Any], runs_dir: Path) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{document['run_id']}.json"
    path.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load_run(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or "metrics" not in document or "config" not in document:
        raise ValueError(f"{path}: not an eval run document")
    return document
