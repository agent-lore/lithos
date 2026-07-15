"""Tests for the build_pipeline composition root (task ba8d7f25).

The factory exists to make two things checkable that were previously only
conventions spread across server.py, cli.py and reconcile.py: the wiring itself,
and the ADR-0006 Slice 1 (#263) rule that exactly one EdgeStore writer backs
edges.db.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lithos.config import LithosConfig
from lithos.pipeline import Pipeline, build_pipeline


class TestBuildPipelineWiring:
    """The graph is wired, and wired to the *same* instances."""

    async def test_returns_every_component_built(self, test_config: LithosConfig) -> None:
        pipeline = await build_pipeline(test_config)
        try:
            assert pipeline.config is test_config
            for name in (
                "knowledge",
                "search",
                "graph",
                "coordination",
                "event_bus",
                "edge_store",
                "projection",
                "intake",
                "memory",
            ):
                assert getattr(pipeline, name) is not None, f"{name} not built"
        finally:
            await pipeline.aclose()

    async def test_one_edge_store_backs_projection_and_intake(
        self, test_config: LithosConfig
    ) -> None:
        """The ADR-0006 invariant, as an assertion rather than a comment.

        The projection owns projection-class rows and CorpusIntake.assert_edge
        owns asserted-class rows; they must share one SQLite handle so there is
        exactly one writer. cli.reindex used to break this by calling
        ProvenanceProjection.create(config) with no injected store.
        """
        pipeline = await build_pipeline(test_config)
        try:
            assert pipeline.projection.edge_store is pipeline.edge_store
            assert pipeline.intake.edge_store is pipeline.edge_store
        finally:
            await pipeline.aclose()

    async def test_collaborators_share_one_instance_each(self, test_config: LithosConfig) -> None:
        """No component gets a private second copy of a collaborator."""
        pipeline = await build_pipeline(test_config)
        try:
            assert pipeline.intake._knowledge is pipeline.knowledge
            assert pipeline.intake._search is pipeline.search
            assert pipeline.intake._graph is pipeline.graph
            assert pipeline.intake._event_bus is pipeline.event_bus
        finally:
            await pipeline.aclose()

    async def test_memory_has_coordination_attached(self, test_config: LithosConfig) -> None:
        """attach_coordination is a transitional setter that memory.start()
        requires; the factory does it so no caller can forget."""
        pipeline = await build_pipeline(test_config)
        try:
            assert pipeline.memory._coordination is pipeline.coordination
        finally:
            await pipeline.aclose()

    async def test_pipeline_is_frozen(self, test_config: LithosConfig) -> None:
        """The wiring is an invariant, not state — no swapping components out
        from under the collaborators that already captured them."""
        pipeline = await build_pipeline(test_config)
        try:
            with pytest.raises(AttributeError):
                pipeline.search = MagicMock()  # type: ignore[misc]
        finally:
            await pipeline.aclose()


class TestBuildPipelineInjection:
    """Pre-built components are adopted, not rebuilt.

    This is what keeps the server's test-injection seam working: server tests
    pre-inject a MagicMock search to skip the real embedding backend.
    """

    async def test_injected_component_is_adopted(self, test_config: LithosConfig) -> None:
        sentinel = MagicMock()
        pipeline = await build_pipeline(test_config, search=sentinel)
        try:
            assert pipeline.search is sentinel
            # ...and the adopted instance is what collaborators are wired to.
            assert pipeline.intake._search is sentinel
        finally:
            await pipeline.aclose()

    async def test_injected_edge_store_is_not_replaced(self, test_config: LithosConfig) -> None:
        """An injected store must reach both holders — otherwise the one-writer
        invariant silently depends on who built the store."""
        from lithos.edge_store import EdgeStore

        store = EdgeStore(test_config)
        await store.open()
        try:
            pipeline = await build_pipeline(test_config, edge_store=store)
            assert pipeline.edge_store is store
            assert pipeline.projection.edge_store is store
            assert pipeline.intake.edge_store is store
        finally:
            await store.close()

    async def test_injected_projection_supplies_the_edge_store(
        self, test_config: LithosConfig
    ) -> None:
        """A projection injected *without* an edge_store must not be left on a
        different handle from the intake.

        The factory used to open a fresh store here and wire the intake to it
        while the injected projection kept its own — two writers against
        edges.db, from the very function introduced to prevent that.
        """
        from lithos.provenance import ProvenanceProjection

        projection = await ProvenanceProjection.create(test_config)
        try:
            pipeline = await build_pipeline(test_config, projection=projection)
            assert pipeline.edge_store is projection.edge_store
            assert pipeline.intake.edge_store is projection.edge_store
        finally:
            await projection.close()

    async def test_injected_intake_supplies_the_edge_store(self, test_config: LithosConfig) -> None:
        """The same defect from the other side: an injected intake already holds
        a store, so the projection must be built against that one."""
        from lithos.edge_store import EdgeStore
        from lithos.intake import CorpusIntake
        from lithos.search import SearchEngine

        store = EdgeStore(test_config)
        await store.open()
        try:
            intake = CorpusIntake(
                knowledge=MagicMock(),
                search=MagicMock(spec=SearchEngine),
                graph=MagicMock(),
                coordination=MagicMock(),
                event_bus=MagicMock(),
                edge_store=store,
            )
            pipeline = await build_pipeline(test_config, intake=intake)
            assert pipeline.edge_store is store
            assert pipeline.projection.edge_store is store
        finally:
            await store.close()

    async def test_contradictory_injection_is_rejected(self, test_config: LithosConfig) -> None:
        """Two holders on two stores cannot be reconciled — fail loudly rather
        than return a valid-looking Pipeline backed by two writers."""
        from lithos.edge_store import EdgeStore
        from lithos.provenance import ProvenanceProjection

        other = EdgeStore(test_config)
        await other.open()
        projection = await ProvenanceProjection.create(test_config)
        try:
            with pytest.raises(ValueError, match="must share one EdgeStore"):
                await build_pipeline(test_config, projection=projection, edge_store=other)
        finally:
            await other.close()
            await projection.close()


class TestPipelineLifecycle:
    async def test_aclose_closes_the_edge_store(self, test_config: LithosConfig) -> None:
        """Short-lived CLI runs must release the aiosqlite handle; a leaked
        worker thread is the #172 'event loop is closed' hang."""
        pipeline = await build_pipeline(test_config)
        await pipeline.aclose()
        assert pipeline.edge_store._db is None

    async def test_aclose_is_idempotent(self, test_config: LithosConfig) -> None:
        pipeline = await build_pipeline(test_config)
        await pipeline.aclose()
        await pipeline.aclose()

    async def test_build_does_not_start_workers(self, test_config: LithosConfig) -> None:
        """The factory builds; it does not run. A CLI command gets the same
        object graph simply by never calling memory.start()."""
        pipeline = await build_pipeline(test_config)
        try:
            assert pipeline.memory._started is False
            assert pipeline.memory._enrich_worker is None
        finally:
            await pipeline.aclose()


class TestPipelineType:
    def test_pipeline_is_not_constructible_without_full_wiring(self) -> None:
        """Every field is required — a Pipeline cannot exist half-built."""
        with pytest.raises(TypeError):
            Pipeline()  # type: ignore[call-arg]
