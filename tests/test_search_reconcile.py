"""Tests for SearchEngine plan_reconcile_to / apply_reconcile (#226)."""

from __future__ import annotations

import pytest

from lithos.config import LithosConfig
from lithos.knowledge import KnowledgeManager
from lithos.search import (
    IndexableDocument,
    ReconcileFailure,
    SearchEngine,
)


def _make_indexable(doc_id: str = "11111111-1111-1111-1111-111111111111") -> IndexableDocument:
    return IndexableDocument(
        id=doc_id,
        title="Reconcile Test",
        content="Body for reconcile test.",
        path="notes/reconcile-test.md",
        author="alice",
        tags=("reconcile",),
        source_url="",
        updated_at="",
        expires_at="",
    )


@pytest.mark.asyncio
async def test_plan_returns_noop_when_corpus_matches(test_config: LithosConfig) -> None:
    """No drift → plan is a noop with zero actions."""
    engine = await SearchEngine.create(test_config)
    indexable = _make_indexable()
    engine.index(indexable)
    # Tantivy may have flipped needs_rebuild during create(); flatten by
    # rebuilding through the public reconcile flow once first.
    engine._tantivy.needs_rebuild = False

    plan = engine.plan_reconcile_to([indexable])

    assert plan.is_noop
    assert plan.actions == ()
    assert plan.scanned == 1


@pytest.mark.asyncio
async def test_plan_reports_schema_mismatch_when_tantivy_needs_rebuild(
    test_config: LithosConfig,
) -> None:
    """Tantivy.needs_rebuild=True surfaces as full_rebuild / schema_mismatch."""
    engine = await SearchEngine.create(test_config)
    engine._tantivy.needs_rebuild = True

    plan = engine.plan_reconcile_to([_make_indexable()])

    tantivy_actions = [a for a in plan.actions if a.backend == "tantivy"]
    assert len(tantivy_actions) == 1
    assert tantivy_actions[0].action == "full_rebuild"
    assert tantivy_actions[0].reason == "schema_mismatch"


@pytest.mark.asyncio
async def test_plan_reports_doc_set_mismatch(test_config: LithosConfig) -> None:
    """Corpus and index disagree → full_rebuild / doc_set_mismatch on both backends."""
    engine = await SearchEngine.create(test_config)
    engine._tantivy.needs_rebuild = False
    # Index nothing; corpus has one doc.
    plan = engine.plan_reconcile_to([_make_indexable()])

    reasons_by_backend = {a.backend: a.reason for a in plan.actions}
    assert reasons_by_backend.get("tantivy") == "doc_set_mismatch"
    assert reasons_by_backend.get("chroma") == "doc_set_mismatch"


@pytest.mark.asyncio
async def test_apply_repairs_drifted_index(test_config: LithosConfig) -> None:
    """apply_reconcile rebuilds both backends so subsequent search finds the doc."""
    engine = await SearchEngine.create(test_config)
    indexable = _make_indexable()
    engine._tantivy.needs_rebuild = False

    plan = engine.plan_reconcile_to([indexable])
    result = engine.apply_reconcile(plan)

    assert result.repaired >= 1
    assert result.failed == ()
    assert any(r.id == indexable.id for r in engine.full_text_search("reconcile"))


@pytest.mark.asyncio
async def test_apply_is_idempotent(test_config: LithosConfig) -> None:
    """Applying the same plan twice leaves the index in the same state."""
    engine = await SearchEngine.create(test_config)
    engine._tantivy.needs_rebuild = False
    indexable = _make_indexable()
    plan = engine.plan_reconcile_to([indexable])

    engine.apply_reconcile(plan)
    engine.apply_reconcile(plan)

    # Index has exactly one document for this id.
    matches = [r for r in engine.full_text_search("reconcile") if r.id == indexable.id]
    assert len(matches) == 1


@pytest.mark.asyncio
async def test_apply_surfaces_per_backend_failures(test_config: LithosConfig) -> None:
    """A single backend failure lands as a ReconcileFailure; the other still repairs."""
    engine = await SearchEngine.create(test_config)
    engine._tantivy.needs_rebuild = False
    indexable = _make_indexable()
    plan = engine.plan_reconcile_to([indexable])

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated tantivy failure")

    engine._tantivy.rebuild_from_docs = _boom  # type: ignore[method-assign]

    result = engine.apply_reconcile(plan)

    assert any(isinstance(f, ReconcileFailure) and f.backend == "tantivy" for f in result.failed)
    # Chroma still got rebuilt.
    assert result.repaired >= 1


@pytest.mark.asyncio
async def test_km_plan_matches_search_engine_plan(test_config: LithosConfig) -> None:
    """KM.plan_reconcile.search slice matches SearchEngine.plan_reconcile_to() for the same corpus."""
    engine = await SearchEngine.create(test_config)
    engine._tantivy.needs_rebuild = False
    knowledge = KnowledgeManager(test_config)

    km_plan = await knowledge.plan_reconcile(engine)

    # Both plans built from the same (empty) corpus see the same noop state.
    direct = engine.plan_reconcile_to([])
    assert km_plan.search.actions == direct.actions
    assert km_plan.search.scanned == direct.scanned


@pytest.mark.asyncio
async def test_km_apply_repairs_drifted_corpus(
    test_config: LithosConfig,
    knowledge_manager: KnowledgeManager,
) -> None:
    """End-to-end: KM.apply_reconcile rebuilds indices to match the on-disk corpus."""
    engine = await SearchEngine.create(test_config)
    engine._tantivy.needs_rebuild = False

    # Seed a real document via the knowledge manager — this writes markdown
    # but does not index, leaving the indices drifted.
    write_result = await knowledge_manager.create(
        title="KM Reconcile Doc",
        content="Drifted content that the indices have never seen.",
        agent="agent",
    )
    assert write_result.document is not None

    plan = await knowledge_manager.plan_reconcile(engine)
    assert not plan.search.is_noop

    result = await knowledge_manager.apply_reconcile(plan, engine)
    assert result.search.failed == ()

    hits = engine.full_text_search("Drifted content")
    assert any(r.id == write_result.document.id for r in hits)
