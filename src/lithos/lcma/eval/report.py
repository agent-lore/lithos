"""Per-model summary of the replayable call log — the canary readout.

Pure functions over ``llm_call_log`` rows: outcome rates, token spend split
(prompt / completion / hidden reasoning), latency percentiles and edges
written per call, grouped by ``(model, reasoning_effort)``. This is what
``lithos eval log-report`` prints while a candidate model runs on staging.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any

from lithos.lcma.call_log import CALL_OUTCOMES


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize_calls(rows: list[dict[str, object]]) -> list[dict[str, Any]]:
    """Group rows by ``(model, reasoning_effort)`` and summarise each group.

    Rates are over all calls in the group; token means are over calls that
    produced a completion (outcome ≠ ``error``), since an errored call spent
    nothing the ledger can see. ``reasoning_share`` is hidden-reasoning
    tokens over completion tokens, only for rows that reported them.
    """
    groups: dict[tuple[str, str | None], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        groups[(str(row["model"]), _opt_str(row.get("reasoning_effort")))].append(row)

    summaries: list[dict[str, Any]] = []
    for (model, effort), group in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        calls = len(group)
        outcomes = {name: 0 for name in sorted(CALL_OUTCOMES)}
        for row in group:
            outcomes[str(row["outcome"])] = outcomes.get(str(row["outcome"]), 0) + 1
        completed = [row for row in group if row["outcome"] != "error"]
        prompt = [float(row["prompt_tokens"] or 0) for row in completed]  # type: ignore[arg-type]
        completion = [float(row["completion_tokens"] or 0) for row in completed]  # type: ignore[arg-type]
        reasoning_rows = [row for row in completed if row.get("reasoning_tokens") is not None]
        reasoning_share = (
            sum(float(row["reasoning_tokens"]) for row in reasoning_rows)  # type: ignore[arg-type]
            / max(1.0, sum(float(row["completion_tokens"] or 0) for row in reasoning_rows))  # type: ignore[arg-type]
            if reasoning_rows
            else None
        )
        latencies = [float(row["latency_ms"] or 0) for row in group]  # type: ignore[arg-type]
        called = sorted(str(row["called_at"]) for row in group)
        summaries.append(
            {
                "model": model,
                "reasoning_effort": effort,
                "calls": calls,
                "first_call": called[0],
                "last_call": called[-1],
                "outcomes": outcomes,
                "rates": {name: count / calls for name, count in outcomes.items()},
                "estimated_share": sum(1 for row in group if row["estimated"]) / calls,
                "prompt_tokens_mean": _mean(prompt),
                "completion_tokens_mean": _mean(completion),
                "total_tokens_mean": _mean(prompt) + _mean(completion),
                "reasoning_share": reasoning_share,
                "latency_ms_p50": median(latencies) if latencies else 0.0,
                "latency_ms_p95": _percentile(latencies, 0.95),
                "edges_per_call": _mean([float(row["edges_written"] or 0) for row in group]),  # type: ignore[arg-type]
            }
        )
    return summaries


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def format_report(summaries: list[dict[str, Any]]) -> str:
    """Plain-text rendering, one block per model."""
    if not summaries:
        return "No LLM calls in the window."
    lines: list[str] = []
    for s in summaries:
        effort = s["reasoning_effort"] or "default"
        lines.append(f"{s['model']} (reasoning_effort={effort})")
        lines.append(f"  calls: {s['calls']}  ({s['first_call'][:19]} .. {s['last_call'][:19]})")
        rates = "  ".join(f"{k}={s['rates'][k]:.1%}" for k in sorted(s["outcomes"]))
        lines.append(f"  outcomes: {rates}")
        share = "n/a" if s["reasoning_share"] is None else f"{s['reasoning_share']:.0%}"
        lines.append(
            f"  tokens/call: prompt {s['prompt_tokens_mean']:.0f}, "
            f"completion {s['completion_tokens_mean']:.0f} "
            f"(reasoning share {share}), estimated {s['estimated_share']:.0%}"
        )
        lines.append(
            f"  latency ms: p50 {s['latency_ms_p50']:.0f}, p95 {s['latency_ms_p95']:.0f}"
            f"   edges/call: {s['edges_per_call']:.2f}"
        )
    return "\n".join(lines)
