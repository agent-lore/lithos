"""Run document and text report for a scored retrieval pool."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lithos.lcma.eval.retrieval.cases import (
    RetrievalCase,
    RetrievalLabel,
    Slot,
    cases_fingerprint,
    labels_fingerprint,
    unlabelled_slots,
)
from lithos.lcma.eval.retrieval.scoring import floor_sweep, score_cases


def build_run_document(
    *,
    cases: Sequence[RetrievalCase],
    labels: Mapping[Slot, RetrievalLabel],
    cases_paths: Sequence[Path],
    labels_paths: Sequence[Path],
    k: int,
    floor: float,
) -> dict[str, Any]:
    roots = []
    for path in cases_paths:
        own = [c for c in cases if c.root == str(path)]
        roots.append(
            {
                "cases_path": str(path),
                "n_cases": len(own),
                "slices": sorted({c.slice for c in own}),
                "cases_fingerprint": cases_fingerprint(own),
                "snapshots": sorted(
                    {json.dumps(c.snapshot, sort_keys=True) for c in own if c.snapshot}
                ),
            }
        )
    return {
        "run_id": f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-retrieval",
        "created_at": datetime.now(UTC).isoformat(),
        "artefact": "retrieval",
        "roots": roots,
        "labels_paths": [str(p) for p in labels_paths],
        "evidence": {
            "cases_fingerprint": cases_fingerprint(cases),
            "labels_fingerprint": labels_fingerprint(labels),
            "n_cases": len(cases),
            "n_labels": len(labels),
            "n_unlabelled_slots": len(unlabelled_slots(cases, labels)),
        },
        "metrics": score_cases(cases, labels, k=k),
        "floor": floor_sweep(cases, labels, floor=floor),
    }


def write_run(document: dict[str, Any], runs_dir: Path) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{document['run_id']}.json"
    path.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}" if abs(value) < 10 else f"{value:.0f}"
    return str(value)


def _metrics_table(block: Mapping[str, Mapping[str, Any]], k: int) -> list[str]:
    columns = [
        ("n", "n_queries"),
        ("n rel", "n_with_relevant"),
        (f"nDCG@{k}", "ndcg_at_k"),
        ("P@3", "precision_at_3"),
        ("P@5", "precision_at_5"),
        ("MRR", "mrr"),
        ("unlabelled", "unlabelled_share"),
        ("ms p50", "latency_ms_median"),
        ("ms p90", "latency_ms_p90"),
    ]
    lines = [
        "| method | " + " | ".join(h for h, _ in columns) + " |",
        "|---|" + "|".join("---:" for _ in columns) + "|",
    ]
    for method, metrics in block.items():
        lines.append(
            f"| {method} | " + " | ".join(_fmt(metrics.get(key)) for _, key in columns) + " |"
        )
    return lines


def format_report(document: Mapping[str, Any]) -> str:
    metrics = document["metrics"]
    k = metrics["k"]
    evidence = document["evidence"]
    lines = [
        f"# Retrieval eval run {document['run_id']}",
        "",
        f"{evidence['n_cases']} cases, {evidence['n_labels']} labels, "
        f"{evidence['n_unlabelled_slots']} pooled pairs still unlabelled.",
        "",
        "## Overall",
        "",
        *_metrics_table(metrics["overall"], k),
    ]
    for slice_name, block in metrics["by_slice"].items():
        lines += ["", f"## Slice: {slice_name}", "", *_metrics_table(block, k)]
    floor = document["floor"]
    lines += [
        "",
        f"## Relevance floor ({floor['n_with_relevant']} queries with a relevant note, "
        f"{floor['n_without_relevant']} with none)",
        "",
        "| signal | floor | hit rate | false supply |",
        "|---|---:|---:|---:|",
    ]
    for name, signal in floor["signals"].items():
        at = signal["at_floor"]
        lines.append(
            f"| {name} | {at['floor']:.2f} | {_fmt(at['hit_rate'])} | "
            f"{_fmt(at['false_supply_rate'])} |"
        )
    return "\n".join(lines) + "\n"
