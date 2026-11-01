"""Deterministic measurement provenance and optional, source-bound evidence."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from typing import Any

from tests.guardrail._common import LANGUAGE, REPO_ROOT, SRC_ROOT, load_architecture, source_files


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def measurement() -> dict:
    arch = load_architecture()
    sources = {
        str(p.relative_to(REPO_ROOT)): hashlib.sha256(
            p.read_text(encoding="utf-8").encode()
        ).hexdigest()
        for p in source_files()
    }
    tools = ["grimp", "networkx"] if LANGUAGE == "python" else ["lizard", "networkx"]
    return {
        "analyzer_version": (REPO_ROOT / "tests/guardrail/KIT_VERSION").read_text().strip(),
        "definitions": {"structure": 1, "complexity": 1, "test_volume": 2},
        "language": LANGUAGE,
        "config_hash": digest(arch),
        "source_hash": digest(sources),
        "tools": {tool: importlib.metadata.version(tool) for tool in tools},
        "scope": {
            "source_root": SRC_ROOT.relative_to(REPO_ROOT).as_posix(),
            "source_suffixes": [".py"]
            if LANGUAGE == "python"
            else [".h", ".hpp", ".c", ".cc", ".cpp"],
            "test_exclusions": [
                "tests/guardrail",
                "tests/test_metrics_diff.py",
                "tests/test_metrics_history.py",
                "tests/test_quality.py",
            ],
            "domain": arch.get("domain", {}),
            "excluded": [
                "third-party code",
                "files outside the source root",
                "templates and browser code",
                "kit tooling",
            ],
        },
        "capabilities": {
            "complexity": "measured",
            "structure": "measured",
            "domain": "measured" if LANGUAGE == "python" else "unsupported",
            "seams": "measured" if LANGUAGE == "python" else "unsupported",
            "mcp": "measured" if arch.get("tool_catalog") else "unsupported",
            **{
                f"size.components.{comp}.{field}": "unsupported"
                for comp in arch["components"]
                for field in ("classes", "functions", "public_symbols")
                if LANGUAGE == "cpp"
            },
        },
    }


def quality_evidence(provenance: dict) -> dict:
    """Optional reports are data, never commands; stale evidence is not scored."""
    config = load_architecture().get("quality", {})
    result: dict[str, Any] = {
        "branch_coverage": {"status": "missing"},
        "mutation": {"status": "missing"},
        "outcomes": config.get("outcomes", []),
    }
    outcomes = result["outcomes"]
    required = ("intervention", "measure", "baseline", "followup", "reference")
    if not isinstance(outcomes, list) or any(
        not isinstance(item, dict)
        or any(not isinstance(item.get(k), str) or not item[k].strip() for k in required)
        or any(type(item.get(k)) not in (int, float) for k in ("before", "after"))
        for item in outcomes
    ):
        result["outcomes"] = []
        result["error"] = (
            "Outcome observations require intervention, measure, windows, reference and numeric before/after values"
        )
    configured = config.get("evidence_path")
    if not configured:
        return result
    try:
        path = (REPO_ROOT / configured).resolve()
        if not path.is_relative_to(REPO_ROOT.resolve()) or path.stat().st_size > 2_000_000:
            raise ValueError("Evidence must be a contained JSON file under 2 MB")
        evidence = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(evidence, dict):
            raise ValueError("Evidence must be an object")
        for key in ("branch_coverage", "mutation"):
            item = evidence.get(key)
            if item is None:
                continue
            if not isinstance(item, dict):
                raise ValueError(f"Invalid {key} evidence")
            covered, total = item.get("covered"), item.get("total")
            if type(covered) is not int or type(total) is not int or not 0 <= covered <= total:
                raise ValueError(f"Invalid {key} counts")
            if not all(
                isinstance(item.get(k), str) and item[k].strip()
                for k in ("tool", "scope", "reference")
            ):
                raise ValueError(f"{key} requires tool, scope and reference")
            status = (
                "measured" if evidence.get("source_hash") == provenance["source_hash"] else "stale"
            )
            result[key] = {
                **item,
                "status": status,
                "value": round(100 * covered / total, 2)
                if total and status == "measured"
                else None,
            }
    except (OSError, ValueError, TypeError) as exc:
        result["error"] = str(exc)
        for key in ("branch_coverage", "mutation"):
            result[key] = {"status": "invalid"}
    return result
