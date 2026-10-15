"""Compare two runs and apply the promotion rule (eval suite E5).

The rule lives in ``evals/POLICY.md`` as prose with one ``toml`` block that
this module reads, so the thresholds and their justification stay in one
file. ``--gate`` turns the comparison into a yes/no with every check listed.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lithos.lcma.eval.scoring import FAILURE_OUTCOMES

#: First values (PRD §E5); the committed POLICY.md overrides them.
DEFAULT_POLICY: dict[str, Any] = {
    "relation_agreement_max_drop": 0.03,
    "direction_agreement_max_drop": 0.05,
    "cost_min_reduction": 0.30,
    "failure_outcomes": list(FAILURE_OUTCOMES),
}

_TOML_BLOCK = re.compile(r"```toml\s*\n(.*?)\n```", re.S)


def load_policy(path: Path) -> dict[str, Any]:
    """Thresholds from the first ``toml`` fenced block in POLICY.md (defaults filled in)."""
    match = _TOML_BLOCK.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"{path}: no ```toml block with the promotion thresholds")
    policy = {**DEFAULT_POLICY, **tomllib.loads(match.group(1))}
    for key in (
        "relation_agreement_max_drop",
        "direction_agreement_max_drop",
        "cost_min_reduction",
    ):
        value = policy[key]
        if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
            raise ValueError(f"{path}: {key} must be a fraction in [0, 1]")
    if not isinstance(policy["failure_outcomes"], list):
        raise ValueError(f"{path}: failure_outcomes must be a list")
    return policy


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class GateResult:
    promotable: bool
    basis: str
    checks: list[Check] = field(default_factory=list)


def _agreement(metrics: dict[str, Any], basis: str, key: str) -> float | None:
    return metrics.get(basis, {}).get(key)


def agreement_basis(incumbent: dict[str, Any], candidate: dict[str, Any]) -> str:
    """Human labels when both runs were scored against them, else the reference."""
    if incumbent.get("labels", {}).get("n_slots") and candidate.get("labels", {}).get("n_slots"):
        return "labels"
    return "reference"


def gate(
    incumbent: dict[str, Any], candidate: dict[str, Any], policy: dict[str, Any] | None = None
) -> GateResult:
    """Apply the promotion rule to two metrics tables."""
    rule = {**DEFAULT_POLICY, **(policy or {})}
    basis = agreement_basis(incumbent, candidate)
    checks: list[Check] = []

    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1%}"

    inc_rel, cand_rel = (_agreement(m, basis, "relation_agreement") for m in (incumbent, candidate))
    rel_ok = (
        inc_rel is not None
        and cand_rel is not None
        and cand_rel >= inc_rel - rule["relation_agreement_max_drop"]
    )
    checks.append(
        Check(
            "relation_agreement",
            rel_ok,
            f"{pct(cand_rel)} vs {pct(inc_rel)} (max drop {rule['relation_agreement_max_drop']:.0%}, basis {basis})",
        )
    )

    inc_dir, cand_dir = (
        _agreement(m, basis, "direction_agreement") for m in (incumbent, candidate)
    )
    if inc_dir is None and cand_dir is None:
        # Both scored on a truth with no directed slots: nothing to compare.
        # (The denominator is the truth's directed slots, so a candidate cannot
        # reach None by answering `none`; one-sided None is a scoring mismatch.)
        dir_ok, detail = True, "no directed slots in the ground truth"
    elif inc_dir is None or cand_dir is None:
        dir_ok = False
        detail = f"{pct(cand_dir)} vs {pct(inc_dir)} (direction measured on one run only)"
    else:
        dir_ok = cand_dir >= inc_dir - rule["direction_agreement_max_drop"]
        detail = f"{pct(cand_dir)} vs {pct(inc_dir)} (max drop {rule['direction_agreement_max_drop']:.0%})"
    checks.append(Check("direction_agreement", dir_ok, detail))

    for name in rule["failure_outcomes"]:
        inc_rate = incumbent.get("rates", {}).get(name) or 0.0
        cand_rate = candidate.get("rates", {}).get(name) or 0.0
        checks.append(
            Check(f"rate:{name}", cand_rate <= inc_rate, f"{pct(cand_rate)} vs {pct(inc_rate)}")
        )

    inc_cost, cand_cost = incumbent.get("cost_per_1k_calls"), candidate.get("cost_per_1k_calls")
    if inc_cost is None or cand_cost is None:
        cost_ok, detail = False, "cost unknown for one run (add the model to evals/prices.toml)"
    elif inc_cost <= 0:
        cost_ok, detail = cand_cost <= inc_cost, f"${cand_cost:.4f} vs ${inc_cost:.4f}"
    else:
        reduction = 1 - cand_cost / inc_cost
        better_agreement = (
            inc_rel is not None
            and cand_rel is not None
            and cand_rel > inc_rel
            and cand_cost <= inc_cost
        )
        cost_ok = reduction >= rule["cost_min_reduction"] or better_agreement
        change = f"{reduction:.0%} cheaper" if reduction >= 0 else f"{-reduction:.0%} dearer"
        detail = (
            f"${cand_cost:.4f} vs ${inc_cost:.4f} per 1k calls ({change}; "
            f"need ≥{rule['cost_min_reduction']:.0%} cheaper, or no dearer with higher agreement)"
        )
    checks.append(Check("cost_per_1k_calls", cost_ok, detail))

    return GateResult(all(c.passed for c in checks), basis, checks)


def evidence_mismatch(run_a: dict[str, Any], run_b: dict[str, Any]) -> list[str]:
    """Reasons two run documents did not score the same evidence (empty = comparable).

    Checks the case-content and label fingerprints and that both runs produced
    a result for every case, so a `--limit` trial, a revised label set or a
    re-exported golden set cannot be gated against a full run by accident.
    """
    a, b = run_a.get("evidence"), run_b.get("evidence")
    reasons: list[str] = []
    for name, ev in (("incumbent", a), ("candidate", b)):
        if not isinstance(ev, dict):
            reasons.append(f"{name} run has no evidence fingerprint (re-run it)")
    if reasons:
        return reasons
    assert isinstance(a, dict) and isinstance(b, dict)
    if a.get("cases_fingerprint") != b.get("cases_fingerprint"):
        reasons.append("runs scored different cases (cases_fingerprint differs)")
    if a.get("labels_fingerprint") != b.get("labels_fingerprint"):
        reasons.append("runs scored against different labels (labels_fingerprint differs)")
    for name, ev in (("incumbent", a), ("candidate", b)):
        ids = ev.get("result_case_ids") or []
        if len(ids) != ev.get("n_cases") or len(set(ids)) != len(ids):
            reasons.append(f"{name} run has results for {len(ids)} of {ev.get('n_cases')} cases")
    if a.get("result_case_ids") != b.get("result_case_ids"):
        reasons.append("runs covered different case ids")
    return reasons


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}" if abs(value) < 10 else f"{value:.1f}"
    return str(value)


def format_run_summary(document: dict[str, Any]) -> str:
    """Plain-text headline for one run: the E3 table in reading order."""
    m = document["metrics"]
    cfg = document.get("config", {})
    lab, ref = m.get("labels", {}), m.get("reference", {})

    def pct(value: Any) -> str:
        return "n/a" if value is None else f"{value:.1%}"

    lines = [
        f"{document.get('run_id', '?')}  model={cfg.get('model')} "
        f"reasoning_effort={cfg.get('reasoning_effort') or 'default'}"
        + ("  [recorded completions]" if document.get("recorded") else ""),
        f"  calls: {m.get('n_calls')} of {m.get('n_cases')} cases",
        "  outcomes: " + "  ".join(f"{k}={pct(v)}" for k, v in m.get("rates", {}).items()),
        f"  contract violations: {pct(m.get('contract_violation_rate'))} of candidate slots",
        f"  vs labels ({lab.get('n_slots', 0)} slots): relation {pct(lab.get('relation_agreement'))}, "
        f"direction {pct(lab.get('direction_agreement'))} ({lab.get('n_directed', 0)} directed)",
        f"  vs reference ({ref.get('n_slots', 0)} slots): relation {pct(ref.get('relation_agreement'))}, "
        f"direction {pct(ref.get('direction_agreement'))}",
        f"  edges/call at floor {m.get('confidence_floor')}: {m.get('edges_per_call_at_floor', 0):.2f}",
        f"  tokens/call: prompt {m.get('tokens', {}).get('prompt_mean', 0):.0f}, "
        f"completion {m.get('tokens', {}).get('completion_mean', 0):.0f}, "
        f"reasoning share {pct(m.get('tokens', {}).get('reasoning_share'))}",
        f"  latency ms: p50 {m.get('latency_ms', {}).get('p50', 0):.0f}, "
        f"p95 {m.get('latency_ms', {}).get('p95', 0):.0f}",
        "  cost per 1k calls: "
        + (
            "n/a (model not in evals/prices.toml)"
            if m.get("cost_per_1k_calls") is None
            else f"${m['cost_per_1k_calls']:.4f}"
        ),
    ]
    calibration = lab.get("calibration")
    if calibration:
        lines.append(
            "  calibration: "
            + "  ".join(
                f"{b}: {pct(v.get('agreement'))} (n={v.get('n')})" for b, v in calibration.items()
            )
        )
    return "\n".join(lines)


def comparison_rows(a: dict[str, Any], b: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    """``(metric, a, b)`` rows in the order the E3 table reports them."""
    rows: list[tuple[str, Any, Any]] = [("calls", a.get("n_calls"), b.get("n_calls"))]
    for basis in ("labels", "reference"):
        for key in ("relation_agreement", "direction_agreement"):
            rows.append((f"{basis}.{key}", _agreement(a, basis, key), _agreement(b, basis, key)))
    rows.append(
        (
            "contract_violation_rate",
            a.get("contract_violation_rate"),
            b.get("contract_violation_rate"),
        )
    )
    for name in ("ok", "partial", "empty", "parse_error", "error"):
        rows.append((f"rate.{name}", a.get("rates", {}).get(name), b.get("rates", {}).get(name)))
    rows.append(
        (
            "edges_per_call_at_floor",
            a.get("edges_per_call_at_floor"),
            b.get("edges_per_call_at_floor"),
        )
    )
    for key in ("prompt_mean", "completion_mean", "reasoning_share"):
        rows.append((f"tokens.{key}", a.get("tokens", {}).get(key), b.get("tokens", {}).get(key)))
    for key in ("p50", "p95"):
        rows.append(
            (
                f"latency_ms.{key}",
                a.get("latency_ms", {}).get(key),
                b.get("latency_ms", {}).get(key),
            )
        )
    rows.append(("cost_per_1k_calls", a.get("cost_per_1k_calls"), b.get("cost_per_1k_calls")))
    return rows


def format_comparison(
    run_a: dict[str, Any], run_b: dict[str, Any], gate_result: GateResult | None = None
) -> str:
    """Markdown: a metrics table for the two runs, then the gate verdict if asked."""
    label_a = run_a.get("run_id", "A")
    label_b = run_b.get("run_id", "B")
    lines = [
        f"| metric | {label_a} | {label_b} |",
        "|---|---:|---:|",
    ]
    for name, va, vb in comparison_rows(run_a["metrics"], run_b["metrics"]):
        lines.append(f"| {name} | {_fmt(va)} | {_fmt(vb)} |")
    mismatch = evidence_mismatch(run_a, run_b)
    lines.append("")
    lines.append(
        "Evidence: same cases, labels and coverage"
        if not mismatch
        else "WARNING — not comparable: " + "; ".join(mismatch)
    )
    if gate_result is not None:
        verdict = "PROMOTABLE" if gate_result.promotable else "NOT PROMOTABLE"
        lines.append("")
        lines.append(f"Gate ({gate_result.basis} basis): **{verdict}**")
        for check in gate_result.checks:
            mark = "pass" if check.passed else "FAIL"
            lines.append(f"- {mark}: {check.name} — {check.detail}")
    return "\n".join(lines)
