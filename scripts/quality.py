"""Shared, dependency-free policy and revision analysis for CI and the hub."""

from __future__ import annotations

import json
from typing import Any

ENTITY_LIMITS = {"function_complexity": 10, "function_length": 75, "module_lines": 800}
MODES = {"invariant", "review", "context"}


def policy_mode(config: dict, key: str) -> str:
    default = "invariant" if key in {"component_cycles", "module_cycles"} else "review"
    return config.get("policies", {}).get(key, default)


def policy_config(architecture: dict) -> dict:
    review = architecture.get("review", {})
    return {
        "unknown_settings": sorted(
            set(review) - set(ENTITY_LIMITS) - {"enforce_new", "enforce_worsened"}
        ),
        "budgets": architecture.get("budgets", {}),
        "policies": architecture.get("policies", {}),
        "limits": {key: review.get(key, value) for key, value in ENTITY_LIMITS.items()},
        "exceptions": architecture.get("exceptions", []),
        "enforce_new": review.get("enforce_new", False),
        "enforce_worsened": review.get("enforce_worsened", False),
    }


def policy_errors(config: dict) -> list[str]:
    errors = []
    if not isinstance(config, dict):
        return ["Review configuration must be a table"]
    for name in ("budgets", "policies", "limits", "actuals"):
        if not isinstance(config.get(name, {}), dict):
            return [f"{name} must be a table"]
    if not isinstance(config.get("exceptions", []), list):
        return ["exceptions must be an array of tables"]
    if config.get("unknown_settings"):
        errors.append(f"Unknown review settings: {config['unknown_settings']}")
    for key, limit in config.get("budgets", {}).items():
        if type(limit) is not int or limit < 0:
            errors.append(f"Invalid budget {key}={limit!r}")
    known = set(config.get("budgets", {})) | set(ENTITY_LIMITS)
    for key, mode in config.get("policies", {}).items():
        if key not in known or mode not in MODES:
            errors.append(f"Invalid policy {key}={mode!r}")
    for key, limit in config.get("limits", {}).items():
        if type(limit) is not int or limit < 0:
            errors.append(f"Invalid threshold {key}={limit!r}")
    for key in ("enforce_new", "enforce_worsened"):
        if type(config.get(key, False)) is not bool:
            errors.append(f"{key} must be a boolean")
    seen = set()
    for exc in config.get("exceptions", []):
        if not isinstance(exc, dict):
            errors.append("Each exception must be a table")
            continue
        metric, target = exc.get("metric"), exc.get("target")
        identity = (metric, target)
        if metric not in ENTITY_LIMITS or not isinstance(target, str) or not target:
            errors.append(f"Invalid exception target: {identity}")
            continue
        if identity in seen:
            errors.append(f"Duplicate exception: {identity}")
        seen.add(identity)
        if policy_mode(config, metric) != "review":
            errors.append(f"Exceptions require a review policy: {identity}")
        if (
            type(exc.get("limit")) is not int
            or exc["limit"] < config.get("limits", ENTITY_LIMITS)[metric]
        ):
            errors.append(f"Exception limit must be at least the threshold: {identity}")
        for field in ("reason", "review"):
            if not isinstance(exc.get(field), str) or not exc[field].strip():
                errors.append(f"Exception {identity} needs {field}")
    return errors


def violations(metrics: dict, config: dict | None = None) -> list[dict]:
    config = config if config is not None else metrics.get("review", {})
    exceptions = {(e["metric"], e["target"]): e for e in config.get("exceptions", [])}
    rows = []
    for metric, section, field in (
        ("function_complexity", "functions", "complexity"),
        ("function_length", "functions", "length"),
        ("module_lines", "modules", "lines"),
    ):
        limit = config.get("limits", {}).get(metric, ENTITY_LIMITS[metric])
        for target, stats in sorted(metrics.get(section, {}).items()):
            actual = stats.get(field)
            if not isinstance(actual, int) or actual <= limit:
                continue
            exc = exceptions.get((metric, target), {})
            mode = policy_mode(config, metric)
            rows.append(
                {
                    "metric": metric,
                    "target": target,
                    "actual": actual,
                    "limit": limit,
                    "policy": mode,
                    "accepted": mode == "review" and actual <= exc.get("limit", -1),
                    "exception_limit": exc.get("limit"),
                    "reason": exc.get("reason", ""),
                    "review": exc.get("review", ""),
                    "path": stats.get("path", ""),
                }
            )
    return rows


def compatibility(old: dict, new: dict) -> list[str]:
    a, b = old.get("measurement", {}), new.get("measurement", {})
    if not a or not b:
        return ["Measurement provenance is missing; historical values are not verified comparable."]
    messages = []
    for key in ("analyzer_version", "definitions", "language", "scope", "config_hash", "tools"):
        if a.get(key) != b.get(key):
            messages.append(
                f"Measurement {key} changed; review this boundary before interpreting trends."
            )
    return messages


def comparable(old: dict, new: dict) -> bool:
    a, b = old.get("measurement", {}), new.get("measurement", {})
    return bool(a and b) and all(
        key in a and key in b and a[key] == b[key]
        for key in ("definitions", "language", "scope", "tools")
    )


def violation_changes(old: dict, new: dict) -> list[dict]:
    # Use the head policy on both revisions: lowering a threshold is a policy
    # change, not a source regression. Exceptions cannot hide measured worsening.
    config = new.get("review", {})
    before = {(r["metric"], r["target"]): r for r in violations(old, config)}
    after = {(r["metric"], r["target"]): r for r in violations(new, config)}
    rows = []
    reliable = comparable(old, new) and "functions" in old and "functions" in new
    for identity in sorted(before.keys() | after.keys()):
        a, b = before.get(identity), after.get(identity)
        if not reliable:
            status = "unverified"
        elif a is None:
            status = "new"
        elif b is None:
            status = "resolved"
        elif b["actual"] > a["actual"]:
            status = "worsened"
        elif b["actual"] < a["actual"]:
            status = "improved"
        else:
            status = "unchanged"
        row = dict(b or a or {})
        section, field = {
            "function_complexity": ("functions", "complexity"),
            "function_length": ("functions", "length"),
            "module_lines": ("modules", "lines"),
        }[identity[0]]
        before_value = old.get(section, {}).get(identity[1], {}).get(field)
        after_value = new.get(section, {}).get(identity[1], {}).get(field)
        row.update(status=status, before=before_value, after=after_value)
        rows.append(row)
    return rows


def _set_changes(before: list, after: list, kind: str) -> list[dict]:
    a = {json.dumps(v, sort_keys=True): v for v in before}
    b = {json.dumps(v, sort_keys=True): v for v in after}
    return [
        {"kind": kind, "status": "removed" if key in a else "added", "item": a.get(key, b.get(key))}
        for key in sorted(a.keys() ^ b.keys())
    ]


def structure_changes(old: dict, new: dict) -> list[dict]:
    changes = []
    for section, field in (
        ("graph", "edges"),
        ("graph", "component_cycles"),
        ("graph", "module_cycles"),
        ("domain", "relationships"),
    ):
        a, b = old.get(section, {}), new.get(section, {})
        if field in a and field in b:
            changes.extend(_set_changes(a[field], b[field], field))
    for section, kind in (
        ("components", "component"),
        ("modules", "module"),
        ("domain_models", "model"),
    ):
        a, b = old.get(section), new.get(section)
        if not isinstance(a, dict) or not isinstance(b, dict):
            continue
        for key in sorted(a.keys() | b.keys()):
            av, bv = a.get(key), b.get(key)
            if kind == "module":
                if (
                    av is not None
                    and bv is not None
                    and ("component" not in av or "component" not in bv)
                ):
                    continue  # missing ownership metadata is not a component move
                av = av.get("component") if av is not None else None
                bv = bv.get("component") if bv is not None else None
            if av != bv:
                status = "added" if key not in a else "removed" if key not in b else "changed"
                changes.append(
                    {"kind": kind, "status": status, "item": key, "before": av, "after": bv}
                )
    return changes


def dependency_diagram(old: dict, new: dict) -> str:
    if "edges" not in old.get("graph", {}) or "edges" not in new.get("graph", {}):
        return ""
    before, after = old.get("graph", {}).get("edges", []), new.get("graph", {}).get("edges", [])
    pairs_a = {(e["source"], e["target"]) for e in before}
    pairs_b = {(e["source"], e["target"]) for e in after}
    evidence_a = {(e["source"], e["target"]): e for e in before}
    evidence_b = {(e["source"], e["target"]): e for e in after}
    names = sorted(set(old.get("components", {})) | set(new.get("components", {})))
    names = sorted(set(names) | {n for pair in pairs_a | pairs_b for n in pair})
    ids = {name: f"n{i}" for i, name in enumerate(names)}
    lines = ["graph TD"]
    for name in names:
        # Mermaid entity escapes avoid directives/HTML supplied by repo labels.
        label = "".join(c if c.isalnum() or c in " _.-" else f"#{ord(c)};" for c in name)
        lines.append(f'  {ids[name]}["{label}"]')
    for i, (src, dst) in enumerate(sorted(pairs_a | pairs_b)):
        lines.append(f"  {ids[src]} --> {ids[dst]}")
        if (src, dst) not in pairs_a:
            lines.append(f"  linkStyle {i} stroke:#198754,stroke-width:3px")
        elif (src, dst) not in pairs_b:
            lines.append(f"  linkStyle {i} stroke:#c62828,stroke-dasharray:5")
        elif evidence_a[(src, dst)] != evidence_b[(src, dst)]:
            lines.append(f"  linkStyle {i} stroke:#b26b00,stroke-width:3px")
    return "\n".join(lines)


def _configuration_changes(old: dict, new: dict) -> list[dict]:
    rows = []
    for field in ("budgets", "policies", "limits", "exceptions", "enforce_new", "enforce_worsened"):
        a, b = old.get("review", {}).get(field), new.get("review", {}).get(field)
        if a != b:
            rows.append({"key": field, "before": a, "after": b})
    return rows


def report(old: dict, new: dict) -> dict:
    warnings = compatibility(old, new)
    if "edges" not in old.get("graph", {}) or "edges" not in new.get("graph", {}):
        warnings.append(
            "Detailed dependency data is unavailable at one revision; no edge comparison is inferred."
        )
    changes = violation_changes(old, new)
    config = new.get("review", {})
    blocked = [
        row
        for row in changes
        if row["after"] is not None
        and row["after"] > row["limit"]
        and (
            row["policy"] == "invariant"
            or (
                row["policy"] == "review"
                and not row["accepted"]
                and (
                    (row["status"] == "new" and config.get("enforce_new", False))
                    or (row["status"] == "worsened" and config.get("enforce_worsened", False))
                )
            )
        )
    ]
    if any(config.get(key) for key in ("enforce_new", "enforce_worsened")) and (
        not comparable(old, new) or "functions" not in old or "functions" not in new
    ):
        warnings.append(
            "Changed-code enforcement requires comparable function snapshots; establish a baseline first."
        )
        blocked.append({"target": "baseline", "metric": "comparability"})
    active = {(row["metric"], row["target"]) for row in violations(new)}
    retired = [e for e in config.get("exceptions", []) if (e["metric"], e["target"]) not in active]
    suggestions = []
    for row in changes:
        if (
            row["accepted"]
            and row["after"] is not None
            and row["status"] != "resolved"
            and row["after"] < row["exception_limit"]
        ):
            suggestions.append(
                f"Review lowering {row['metric']} exception for {row['target']} to {row['after']}."
            )
    suggestions.extend(
        f"Review removing exception {e['metric']} for {e['target']}." for e in retired
    )
    for key, limit in config.get("budgets", {}).items():
        actual = config.get("actuals", {}).get(key)
        if actual is None:
            warnings.append(f"Budget {key} has no supported measurement.")
            if policy_mode(config, key) == "invariant":
                blocked.append({"target": "measurement", "metric": key})
            continue
        mode = policy_mode(config, key)
        if actual > limit and mode == "invariant":
            blocked.append({"target": "repository", "metric": key})
        if 0 <= actual < limit and mode != "context":
            suggestions.append(
                f"Review lowering budget {key}: actual {actual}, accepted ceiling {limit}."
            )
    return {
        "budgets": [
            {
                "key": key,
                "limit": value,
                "actual": config.get("actuals", {}).get(key),
                "policy": policy_mode(config, key),
            }
            for key, value in sorted(config.get("budgets", {}).items())
        ],
        "measurements": {"before": old.get("measurement", {}), "after": new.get("measurement", {})},
        "warnings": warnings,
        "violations": changes,
        "structure": structure_changes(old, new),
        "configuration": _configuration_changes(old, new),
        "evidence_changes": _set_changes(
            old.get("quality", {}).get("outcomes", []),
            new.get("quality", {}).get("outcomes", []),
            "outcome",
        ),
        "suggestions": suggestions,
        "blocked": blocked,
        "diagram": dependency_diagram(old, new),
        "quality": new.get("quality", {}),
    }


def render_report(result: dict) -> str:
    def cell(value: Any) -> str:
        if value is None:
            return "—"
        text = (
            json.dumps(value, sort_keys=True, ensure_ascii=False)
            if isinstance(value, (dict, list))
            else str(value)
        )
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("|", "&#124;")
            .replace("`", "&#96;")
            .replace("\n", " ")
        )

    lines = ["### Quality review", ""]
    lines.extend(f"- {cell(w)}" for w in result["warnings"])
    lines += [
        "",
        "| Status | Metric | Target | Before | After | Policy | Exception |",
        "|---|---|---|---:|---:|---|---|",
    ]
    lines.extend(
        "| "
        + " | ".join(
            cell(row[key]) for key in ("status", "metric", "target", "before", "after", "policy")
        )
        + " | "
        + (cell(row["reason"]) + " (" + cell(row["review"]) + ")" if row["accepted"] else "—")
        + " |"
        for row in result["violations"]
    )
    for row in result["budgets"]:
        if row["actual"] is not None and row["actual"] > row["limit"]:
            lines.append(
                f"- {row['policy']} budget {row['key']}: {row['actual']} exceeds {row['limit']}."
            )
    for row in result["structure"]:
        lines.append(f"- {row['kind']} {row['status']}: {cell(row['item'])}")
        if "before" in row:
            lines.append(f"  {cell(row['before'])} → {cell(row['after'])}")
    for key in ("branch_coverage", "mutation"):
        evidence = result.get("quality", {}).get(key, {})
        lines.append(
            f"- {key}: {cell(evidence.get('status', 'missing'))}; value {cell(evidence.get('value'))}; scope {cell(evidence.get('scope', 'not recorded'))}; evidence {cell(evidence.get('reference', 'not recorded'))}"
        )
    for outcome in result.get("quality", {}).get("outcomes", []):
        lines.append(f"- Observed outcome: {cell(outcome)} (observation, not causal proof)")
    for row in result["configuration"]:
        lines.append(
            f"- Policy/configuration {row['key']}: {cell(row['before'])} → {cell(row['after'])}"
        )
    lines.extend(f"- {cell(s)}" for s in result["suggestions"])
    if result["blocked"]:
        lines.append("\nConfigured quality policies require review before this change can pass.")
    return "\n".join(lines) + "\n"
