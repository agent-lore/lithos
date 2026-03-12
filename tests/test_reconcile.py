"""Tests for reconcile module - Phase 6 drift detection and repair.

Tests cover all 8 required scenarios plus 3 additional drift-detection tests:
1. indices dry-run: reports planned full rebuild without mutating indices
2. indices real run: rebuilds and makes search results match markdown corpus
3. graph dry-run: reports repair actions without touching cache files
4. graph real run: rebuilds broken cache and is idempotent on rerun
5. provenance_projection: returns deterministic no-op before projection store exists
6. provenance_projection: returns supported=True when edges.db exists
7. scope="all": aggregates mixed success into partial_failure correctly
8. crash during one scope: does not corrupt markdown and rerun succeeds
9. indices: same-count/wrong-doc drift detected via doc-id set comparison
10. graph: edge-set drift detected when wiki-links change but doc set stays same
11. dry-run summary counts reflect planned actions (repaired > 0)
"""

import pytest

import lithos.reconcile as reconcile_module
from lithos.config import LithosConfig
from lithos.knowledge import KnowledgeManager
from lithos.reconcile import reconcile
from lithos.search import SearchEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_doc(knowledge: KnowledgeManager, title: str, content: str) -> str:
    """Create a doc and return its ID."""
    result = await knowledge.create(title=title, content=content, agent="test")
    assert result.status == "created"
    assert result.document is not None
    return result.document.id


# ---------------------------------------------------------------------------
# Test 1 — indices dry-run: plans rebuild without mutating indices
# ---------------------------------------------------------------------------


async def test_indices_dry_run_no_mutation(
    test_config: LithosConfig, knowledge_manager: KnowledgeManager
) -> None:
    """Dry-run reports a planned full_rebuild but does NOT populate the index."""
    await _create_doc(knowledge_manager, "Alpha Doc", "Content for alpha document.")

    result = await reconcile(scope="indices", dry_run=True, config=test_config)

    assert result["dry_run"] is True
    assert result["scope"] == "indices"
    # There are pending actions (doc not indexed yet) — status is "ok" (planned)
    assert result["status"] == "ok"
    assert any(a.get("action") == "full_rebuild" for a in result["actions"])

    # Verify the index was NOT populated (no mutation on dry-run)
    search = SearchEngine(test_config)
    ft_results = search.full_text_search("alpha document")
    assert len(ft_results) == 0


# ---------------------------------------------------------------------------
# Test 2 — indices real run: rebuilds so search finds the corpus docs
# ---------------------------------------------------------------------------


async def test_indices_real_run_rebuilds_index(
    test_config: LithosConfig, knowledge_manager: KnowledgeManager
) -> None:
    """Real run indexes the corpus; search returns expected results afterward."""
    doc_id = await _create_doc(
        knowledge_manager, "Beta Doc", "Unique content for beta integration test."
    )

    # Index is empty before reconcile
    search_before = SearchEngine(test_config)
    assert len(search_before.full_text_search("beta integration")) == 0

    result = await reconcile(scope="indices", dry_run=False, config=test_config)

    assert result["dry_run"] is False
    assert result["scope"] == "indices"
    assert result["status"] == "ok"
    assert result["summary"]["repaired"] > 0

    # Search should now find the document
    search_after = SearchEngine(test_config)
    ft_results = search_after.full_text_search("beta integration")
    assert any(r.id == doc_id for r in ft_results)


# ---------------------------------------------------------------------------
# Test 3 — graph dry-run: reports repair without creating cache file
# ---------------------------------------------------------------------------


async def test_graph_dry_run_no_cache_written(
    test_config: LithosConfig, knowledge_manager: KnowledgeManager
) -> None:
    """Graph dry-run reports a planned rebuild but does not write the cache."""
    await _create_doc(knowledge_manager, "Gamma Doc", "Gamma content [[delta-doc]].")

    from lithos.graph import KnowledgeGraph

    graph = KnowledgeGraph(test_config)
    cache_path = graph.graph_cache_path
    assert not cache_path.exists(), "Cache should not exist before the test"

    result = await reconcile(scope="graph", dry_run=True, config=test_config)

    assert result["dry_run"] is True
    assert result["scope"] == "graph"
    assert result["status"] == "ok"
    assert any(a.get("action") == "full_rebuild" for a in result["actions"])

    # Cache must still not exist
    assert not cache_path.exists()


# ---------------------------------------------------------------------------
# Test 4 — graph real run: rebuilds cache and is idempotent
# ---------------------------------------------------------------------------


async def test_graph_real_run_idempotent(
    test_config: LithosConfig, knowledge_manager: KnowledgeManager
) -> None:
    """First graph run rebuilds; second run with no changes returns noop."""
    await _create_doc(knowledge_manager, "Gamma Doc", "Gamma content.")
    await _create_doc(knowledge_manager, "Delta Doc", "Delta content [[gamma-doc]].")

    from lithos.graph import KnowledgeGraph

    graph = KnowledgeGraph(test_config)
    assert not graph.graph_cache_path.exists()

    # First run: should rebuild
    result1 = await reconcile(scope="graph", dry_run=False, config=test_config)
    assert result1["status"] == "ok"
    assert result1["summary"]["repaired"] > 0
    assert graph.graph_cache_path.exists()

    # Second run with no changes: should be noop
    result2 = await reconcile(scope="graph", dry_run=False, config=test_config)
    assert result2["status"] == "noop"
    assert result2["summary"]["repaired"] == 0
    assert result2["actions"] == []


# ---------------------------------------------------------------------------
# Test 5 — provenance_projection: deterministic noop before store exists
# ---------------------------------------------------------------------------


async def test_provenance_projection_no_store(test_config: LithosConfig) -> None:
    """Returns supported=False, status=noop deterministically when edges.db absent."""
    edges_db = test_config.storage.data_dir / "edges.db"
    assert not edges_db.exists()

    result1 = await reconcile(scope="provenance_projection", dry_run=False, config=test_config)
    result2 = await reconcile(scope="provenance_projection", dry_run=True, config=test_config)

    for result in (result1, result2):
        assert result["scope"] == "provenance_projection"
        assert result["supported"] is False
        assert result["status"] == "noop"
        assert result["summary"]["scanned"] == 0
        assert result["summary"]["repaired"] == 0
        assert result["summary"]["failed"] == 0

    # Both calls must return the same shape (deterministic)
    assert result1["supported"] == result2["supported"]
    assert result1["status"] == result2["status"]


# ---------------------------------------------------------------------------
# Test 6 — provenance_projection: supported=True when edges.db exists
# ---------------------------------------------------------------------------


async def test_provenance_projection_store_present(test_config: LithosConfig) -> None:
    """When edges.db is present the scope is supported and returns noop (no LCMA yet)."""
    edges_db = test_config.storage.data_dir / "edges.db"
    edges_db.touch()

    result = await reconcile(scope="provenance_projection", dry_run=False, config=test_config)

    assert result["scope"] == "provenance_projection"
    assert result["supported"] is True
    assert result["status"] == "noop"
    assert result["failures"] == []


# ---------------------------------------------------------------------------
# Test 7 — scope="all": aggregates mixed results into partial_failure
# ---------------------------------------------------------------------------


async def test_all_scope_partial_failure(
    test_config: LithosConfig,
    knowledge_manager: KnowledgeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When one sub-scope fails and others succeed, all returns partial_failure."""
    await _create_doc(knowledge_manager, "Epsilon Doc", "Epsilon content.")

    async def failing_indices(config: LithosConfig, dry_run: bool) -> dict:
        return reconcile_module._make_result(
            "indices",
            dry_run,
            status="failed",
            failed=1,
            failures=[{"code": "index_rebuild_failed", "detail": "injected test error"}],
        )

    monkeypatch.setattr(reconcile_module, "_reconcile_indices", failing_indices)

    result = await reconcile(scope="all", dry_run=False, config=test_config)

    assert result["scope"] == "all"
    assert result["status"] == "partial_failure"
    # At least one failure recorded
    assert result["summary"]["failed"] >= 1
    assert any(f.get("code") == "index_rebuild_failed" for f in result["failures"])


# ---------------------------------------------------------------------------
# Test 8 — crash safety: source-of-truth never mutated; rerun succeeds
# ---------------------------------------------------------------------------


async def test_crash_safety_markdown_never_mutated(
    test_config: LithosConfig,
    knowledge_manager: KnowledgeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure during reconcile does not corrupt markdown; rerun succeeds."""
    doc_id = await _create_doc(knowledge_manager, "Zeta Doc", "Zeta content for crash safety test.")

    # Read the raw markdown content before any reconcile
    doc_before, _ = await knowledge_manager.read(id=doc_id)
    raw_before = doc_before.content

    # Inject a failure during graph cache save (simulates crash mid-apply)
    original_save = reconcile_module.KnowledgeGraph.save_cache

    def crashing_save(self: reconcile_module.KnowledgeGraph) -> None:
        raise RuntimeError("simulated crash during save_cache")

    monkeypatch.setattr(reconcile_module.KnowledgeGraph, "save_cache", crashing_save)

    # Graph reconcile should fail gracefully
    result_crash = await reconcile(scope="graph", dry_run=False, config=test_config)
    assert result_crash["status"] == "failed"

    # Markdown must be unchanged
    doc_after_crash, _ = await knowledge_manager.read(id=doc_id)
    assert doc_after_crash.content == raw_before, "Markdown must never be mutated by reconcile"

    # Restore the original save_cache
    monkeypatch.setattr(reconcile_module.KnowledgeGraph, "save_cache", original_save)

    # Re-run without the injected failure — should now succeed
    result_rerun = await reconcile(scope="graph", dry_run=False, config=test_config)
    assert result_rerun["status"] == "ok"
    assert result_rerun["summary"]["repaired"] > 0


# ---------------------------------------------------------------------------
# Test 9 — indices: same-count/wrong-doc drift via doc-id set comparison
# ---------------------------------------------------------------------------


async def test_indices_detects_wrong_doc_same_count(
    test_config: LithosConfig, knowledge_manager: KnowledgeManager
) -> None:
    """Index with same doc count but different IDs is detected as drifted."""
    doc_id_a = await _create_doc(knowledge_manager, "Doc A", "Content A.")
    await _create_doc(knowledge_manager, "Doc B", "Content B.")

    # Build indices so they match the current corpus
    result_init = await reconcile(scope="indices", dry_run=False, config=test_config)
    assert result_init["status"] == "ok"

    # Verify clean state
    result_clean = await reconcile(scope="indices", dry_run=False, config=test_config)
    assert result_clean["status"] == "noop"

    # Delete one doc and create a new one — same count, different IDs
    await knowledge_manager.delete(doc_id_a)
    await _create_doc(knowledge_manager, "Doc C", "Content C.")

    # Reconcile should detect drift (not noop)
    result_drift = await reconcile(scope="indices", dry_run=False, config=test_config)
    assert result_drift["status"] == "ok"
    assert result_drift["summary"]["repaired"] > 0
    assert any(a.get("reason") == "doc_set_mismatch" for a in result_drift["actions"])


# ---------------------------------------------------------------------------
# Test 10 — graph: edge-set drift when wiki-links change
# ---------------------------------------------------------------------------


async def test_graph_detects_edge_set_drift(
    test_config: LithosConfig, knowledge_manager: KnowledgeManager
) -> None:
    """Graph drift is detected when wiki-links change but document set stays the same."""
    doc_id = await _create_doc(knowledge_manager, "Link Source", "Points to [[target-a]].")
    await _create_doc(knowledge_manager, "Target A", "Target A content.")

    # Build graph cache
    result_init = await reconcile(scope="graph", dry_run=False, config=test_config)
    assert result_init["status"] == "ok"

    # Verify clean state
    result_clean = await reconcile(scope="graph", dry_run=False, config=test_config)
    assert result_clean["status"] == "noop"

    # Change wiki-links without adding/removing documents
    await knowledge_manager.update(
        id=doc_id, agent="test", content="Now points to [[target-b]] instead."
    )

    # Reconcile should detect edge drift (not noop)
    result_drift = await reconcile(scope="graph", dry_run=False, config=test_config)
    assert result_drift["status"] == "ok"
    assert result_drift["summary"]["repaired"] > 0
    assert any(a.get("reason") == "edge_set_mismatch" for a in result_drift["actions"])


# ---------------------------------------------------------------------------
# Test 11 — dry-run summary counts reflect planned actions
# ---------------------------------------------------------------------------


async def test_dry_run_summary_reflects_planned_actions(
    test_config: LithosConfig, knowledge_manager: KnowledgeManager
) -> None:
    """Dry-run summary.repaired matches len(actions), not zero."""
    await _create_doc(knowledge_manager, "Dry Run Doc", "Content for dry-run test.")

    # indices dry-run: docs not indexed yet, so actions are pending
    result_idx = await reconcile(scope="indices", dry_run=True, config=test_config)
    assert result_idx["status"] == "ok"
    assert len(result_idx["actions"]) > 0
    assert result_idx["summary"]["repaired"] == len(result_idx["actions"])

    # graph dry-run: no cache yet, so rebuild is planned
    result_graph = await reconcile(scope="graph", dry_run=True, config=test_config)
    assert result_graph["status"] == "ok"
    assert len(result_graph["actions"]) > 0
    assert result_graph["summary"]["repaired"] == len(result_graph["actions"])


# ---------------------------------------------------------------------------
# Test 12-14 — stale wiki-link detection in graph scope
# ---------------------------------------------------------------------------


class TestReconcileStaleWikiLinks:
    async def test_stale_link_detected_in_dry_run(
        self, test_config: LithosConfig, knowledge_manager: KnowledgeManager
    ) -> None:
        """Dry-run detects and reports stale wiki-link without writing cache."""
        doc_a_id = await _create_doc(knowledge_manager, "Doc A", "Links to [[NonExistentNote]].")

        # Build a consistent cache first
        build_result = await reconcile(scope="graph", dry_run=False, config=test_config)
        assert build_result["status"] == "ok"

        # Dry-run should detect the stale link
        result = await reconcile(scope="graph", dry_run=True, config=test_config)
        assert result["status"] == "ok"

        stale_actions = [a for a in result["actions"] if a.get("action") == "stale_link"]
        assert len(stale_actions) == 1
        assert stale_actions[0]["source_id"] == doc_a_id
        assert stale_actions[0]["link_target"] == "NonExistentNote"

    async def test_stale_link_reported_in_real_run(
        self, test_config: LithosConfig, knowledge_manager: KnowledgeManager
    ) -> None:
        """Real run reports stale wiki-link but does NOT modify note content."""
        doc_a_id = await _create_doc(knowledge_manager, "Doc A", "Links to [[NonExistentNote]].")

        # Build a consistent cache first
        build_result = await reconcile(scope="graph", dry_run=False, config=test_config)
        assert build_result["status"] == "ok"

        # Real run should report stale link
        result = await reconcile(scope="graph", dry_run=False, config=test_config)
        assert result["status"] == "ok"

        stale_actions = [a for a in result["actions"] if a.get("action") == "stale_link"]
        assert len(stale_actions) == 1
        assert stale_actions[0]["source_id"] == doc_a_id
        assert stale_actions[0]["link_target"] == "NonExistentNote"

        # Note content must NOT have been modified
        doc, _ = await knowledge_manager.read(id=doc_a_id)
        assert "[[NonExistentNote]]" in doc.content

    async def test_no_stale_links_when_all_resolve(
        self, test_config: LithosConfig, knowledge_manager: KnowledgeManager
    ) -> None:
        """No stale_link actions when all wiki-links resolve to existing documents."""
        await _create_doc(knowledge_manager, "Doc A", "Links to [[doc-b]].")
        await _create_doc(knowledge_manager, "Doc B", "Content of doc B.")

        # Build cache
        build_result = await reconcile(scope="graph", dry_run=False, config=test_config)
        assert build_result["status"] == "ok"

        # Second run: all links resolve, no stale links → noop
        result = await reconcile(scope="graph", dry_run=False, config=test_config)
        assert result["status"] == "noop"

        stale_actions = [a for a in result["actions"] if a.get("action") == "stale_link"]
        assert len(stale_actions) == 0
