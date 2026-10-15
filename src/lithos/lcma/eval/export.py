"""Fixture export from the replayable call log (E1 → E2 bridge).

One case per logged call whose completion is usable (``ok``/``partial``):
the verbatim messages, the structured focus/candidate inputs, and the
incumbent's completion as the *reference* judgement. Cases are keyed by the
request digest, so re-adjudications of unchanged content dedupe to one case
(the latest call wins) and a case id is stable across exports.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any

from lithos.lcma.call_log import REPLAYABLE_OUTCOMES, BlobStore
from lithos.lcma.edge_inference import parse_adjudications
from lithos.lcma.stats import StatsStore

#: Characters of the request digest used as the case id.
CASE_ID_CHARS = 16


def case_from_call(
    row: dict[str, object], request: dict[str, Any], completion: str
) -> dict[str, Any]:
    """Shape one logged call into a golden-set case (pure)."""
    candidates = request.get("candidates", [])
    parsed = parse_adjudications(completion, len(candidates))
    judgements: list[dict[str, Any]] | None = None
    problems: int | None = None
    if parsed is not None:
        adjudications, problems = parsed
        judgements = [asdict(a) for a in adjudications]
    request_sha = str(row["request_sha"])
    return {
        "case_id": request_sha[:CASE_ID_CHARS],
        "source": {
            "call_id": row["id"],
            "node_id": row["node_id"],
            "doc_version": row["doc_version"],
            "inference_version": row["inference_version"],
            "called_at": row["called_at"],
            "request_sha": request_sha,
        },
        "prompt_version": request.get("prompt_version"),
        "snippet_chars": request.get("snippet_chars"),
        "focus": request.get("focus"),
        "candidates": candidates,
        "messages": request.get("messages"),
        "reference": {
            "model": row["model"],
            "reasoning_effort": row["reasoning_effort"],
            "outcome": row["outcome"],
            "completion": completion,
            "judgements": judgements,
            "problems": problems,
            "prompt_tokens": row["prompt_tokens"],
            "completion_tokens": row["completion_tokens"],
            "reasoning_tokens": row["reasoning_tokens"],
            "latency_ms": row["latency_ms"],
        },
    }


async def export_cases(
    store: StatsStore,
    blobs: BlobStore,
    *,
    since: datetime | None,
    model: str | None = None,
    shadow: bool = False,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Cases newest-first plus the number of rows skipped for a missing blob.

    Rows are read newest-first and deduped on ``request_sha``, so the latest
    call for identical inputs is the one exported. *limit* bounds the cases
    returned, not the rows scanned.
    """
    rows = await store.list_llm_calls(
        since=since, model=model, shadow=shadow, outcomes=REPLAYABLE_OUTCOMES
    )
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    missing = 0
    for row in rows:
        request_sha = str(row["request_sha"])
        if request_sha in seen:
            continue
        completion_sha = row["completion_sha"]
        request_bytes = blobs.get(request_sha)
        completion_bytes = blobs.get(str(completion_sha)) if completion_sha else None
        if request_bytes is None or completion_bytes is None:
            missing += 1
            continue
        seen.add(request_sha)
        request = json.loads(request_bytes.decode("utf-8"))
        cases.append(case_from_call(row, request, completion_bytes.decode("utf-8")))
        if limit is not None and len(cases) >= limit:
            break
    return cases, missing
