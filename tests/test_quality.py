"""Semantic policy regression tests shared with installed kits."""

from __future__ import annotations

from copy import deepcopy

from scripts.quality import policy_config, policy_errors, report, violations


def snapshot(score: int = 11) -> dict:
    return {
        "measurement": {
            "definitions": {"complexity": 1},
            "language": "python",
            "scope": {"source_root": "src/pkg"},
            "tools": {},
        },
        "functions": {"pkg.work": {"complexity": score, "length": 20, "path": "src/pkg.py"}},
        "modules": {},
        "graph": {"edges": [], "component_cycles": []},
        "components": {},
        "review": policy_config({"review": {"enforce_worsened": True}}),
    }


def test_worsening_existing_function_cannot_hide_below_global_maximum() -> None:
    result = report(snapshot(11), snapshot(18))
    assert result["violations"][0]["status"] == "worsened"
    assert result["blocked"][0]["target"] == "pkg.work"


def test_bounded_exception_accepts_growth_only_up_to_its_limit() -> None:
    old, new = snapshot(), snapshot(18)
    new["review"]["exceptions"] = [
        {
            "metric": "function_complexity",
            "target": "pkg.work",
            "limit": 18,
            "reason": "One cohesive parser",
            "review": "PR #42",
        }
    ]
    assert not report(old, new)["blocked"]
    new["functions"]["pkg.work"]["complexity"] = 19
    assert report(old, new)["blocked"]


def test_retired_exception_is_suggested_for_removal_without_none_comparison() -> None:
    old, new = snapshot(), snapshot(3)
    new["review"]["exceptions"] = [
        {
            "metric": "function_complexity",
            "target": "pkg.work",
            "limit": 18,
            "reason": "Cohesion",
            "review": "PR #42",
        }
    ]
    result = report(old, new)
    assert result["violations"][0]["status"] == "resolved"
    assert "removing exception" in result["suggestions"][0]


def test_metric_definition_change_does_not_claim_improvement() -> None:
    old, new = snapshot(), snapshot(3)
    new["measurement"]["definitions"]["complexity"] = 2
    result = report(old, new)
    assert result["violations"][0]["status"] == "unverified"
    assert result["blocked"][0]["metric"] == "comparability"


def test_policy_threshold_change_is_not_source_regression() -> None:
    old, new = snapshot(), snapshot()
    old["review"]["limits"]["function_complexity"] = 20
    assert report(old, new)["violations"][0]["status"] == "unchanged"


def test_review_is_nonblocking_by_default_but_invariants_are_enforced() -> None:
    old, new = snapshot(3), snapshot(15)
    assert not report(old, new)["blocked"]
    new["review"]["policies"]["function_complexity"] = "invariant"
    assert report(old, new)["blocked"]


def test_invalid_and_duplicate_exceptions_fail_validation() -> None:
    config = snapshot()["review"]
    config["exceptions"] = [
        {
            "metric": "function_complexity",
            "target": "pkg.work",
            "limit": 9,
            "reason": "",
            "review": "",
        }
    ] * 2
    errors = policy_errors(config)
    assert any("Duplicate" in e for e in errors)
    assert any("reason" in e for e in errors)
    assert any("threshold" in e for e in errors)


def test_changed_imports_with_same_component_pair_are_visible() -> None:
    old, new = snapshot(), snapshot()
    old["graph"]["edges"] = [{"source": "A", "target": "B", "imports": ["pkg.a -> pkg.b"]}]
    new["graph"]["edges"] = [{"source": "A", "target": "B", "imports": ["pkg.a2 -> pkg.b"]}]
    result = report(old, new)
    assert [r["status"] for r in result["structure"]] == ["removed", "added"]


def test_context_violations_are_visible_but_not_gated() -> None:
    old, new = snapshot(3), snapshot()
    new["review"]["enforce_new"] = True
    new["review"]["policies"]["function_complexity"] = "context"
    assert violations(new)[0]["policy"] == "context"
    assert not report(old, new)["blocked"]


def test_cycle_membership_changes_and_component_moves_are_reported() -> None:
    old = snapshot()
    old["graph"]["component_cycles"] = [["A", "B"]]
    old["modules"] = {"pkg.work": {"component": "A", "lines": 20}}
    new = deepcopy(old)
    new["graph"]["component_cycles"] = [["B", "C"]]
    new["modules"]["pkg.work"]["component"] = "C"
    result = report(old, new)
    assert sum(r["kind"] == "component_cycles" for r in result["structure"]) == 2
    assert any(r["kind"] == "module" and r["status"] == "changed" for r in result["structure"])


def test_resolved_invariant_is_not_blocked_and_shows_measured_after_value() -> None:
    old, new = snapshot(11), snapshot(3)
    new["review"]["policies"]["function_complexity"] = "invariant"
    result = report(old, new)
    assert result["violations"][0]["after"] == 3
    assert not result["blocked"]


def test_new_violation_shows_actual_base_even_when_it_was_below_threshold() -> None:
    result = report(snapshot(3), snapshot(11))
    assert result["violations"][0]["before"] == 3
    assert result["violations"][0]["status"] == "new"


def test_partial_provenance_cannot_establish_comparability() -> None:
    old, new = snapshot(), snapshot(3)
    old["measurement"] = new["measurement"] = {"analyzer_version": "1"}
    assert report(old, new)["violations"][0]["status"] == "unverified"


def test_legacy_missing_metadata_is_not_reported_as_added_edges_or_module_moves() -> None:
    old, new = snapshot(), snapshot()
    old["graph"].pop("edges")
    new["graph"]["edges"] = [{"source": "A", "target": "B", "imports": ["a -> b"]}]
    old["modules"] = {"pkg.work": {"lines": 20}}
    new["modules"] = {"pkg.work": {"lines": 20, "component": "A"}}
    result = report(old, new)
    assert not result["diagram"]
    assert not result["structure"]
    assert any("unavailable" in warning for warning in result["warnings"])
