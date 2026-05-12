"""Tests for the CognitiveMemory Module scaffold and lifecycle (ADR-0005).

This slice (issue #255) only exercises construction and start/stop. Public
read/write methods migrate in subsequent slices (#257-#260); their tests
land with those changes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from lithos.cognitive_memory import CognitiveMemory
from lithos.config import LithosConfig
from lithos.events import EventBus
from lithos.provenance import ProvenanceProjection


@pytest_asyncio.fixture
async def projection(test_config: LithosConfig):
    proj = await ProvenanceProjection.create(test_config)
    try:
        yield proj
    finally:
        await proj.close()


@pytest.fixture
def event_bus(test_config: LithosConfig) -> EventBus:
    return EventBus(test_config.events)


@pytest.fixture
def mock_knowledge() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_search() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_graph() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_coordination() -> AsyncMock:
    return AsyncMock()


@pytest_asyncio.fixture
async def memory(
    test_config: LithosConfig,
    mock_knowledge: MagicMock,
    mock_search: MagicMock,
    mock_graph: MagicMock,
    projection: ProvenanceProjection,
    event_bus: EventBus,
    mock_coordination: AsyncMock,
):
    """Construct a CognitiveMemory and guarantee teardown on test failure."""
    cm = await CognitiveMemory.create(
        config=test_config,
        knowledge=mock_knowledge,
        search=mock_search,
        graph=mock_graph,
        projection=projection,
        event_bus=event_bus,
        coordination=mock_coordination,
    )
    try:
        yield cm
    finally:
        await cm.stop()


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestCreate:
    """``CognitiveMemory.create`` wires deps without opening stores."""

    async def test_create_constructs_module(self, memory: CognitiveMemory) -> None:
        assert memory._stats_store is not None
        # ``open()`` is deferred to ``start()`` — fresh store reports closed.
        assert memory._stats_store._opened is False
        assert memory._enrich_worker is None
        assert memory._started is False

    async def test_create_stores_dependencies(
        self,
        test_config: LithosConfig,
        mock_knowledge: MagicMock,
        mock_search: MagicMock,
        mock_graph: MagicMock,
        projection: ProvenanceProjection,
        event_bus: EventBus,
        mock_coordination: AsyncMock,
    ) -> None:
        cm = await CognitiveMemory.create(
            config=test_config,
            knowledge=mock_knowledge,
            search=mock_search,
            graph=mock_graph,
            projection=projection,
            event_bus=event_bus,
            coordination=mock_coordination,
        )
        try:
            assert cm._config is test_config
            assert cm._knowledge is mock_knowledge
            assert cm._search is mock_search
            assert cm._graph is mock_graph
            assert cm._projection is projection
            assert cm._event_bus is event_bus
            assert cm._coordination is mock_coordination
        finally:
            await cm.stop()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    """``start`` opens the StatsStore and starts the worker; ``stop`` reverses."""

    async def test_start_opens_stats_store_and_starts_worker(self, memory: CognitiveMemory) -> None:
        await memory.start()

        assert memory._started is True
        assert memory._stats_store._opened is True

        worker = memory._enrich_worker
        assert worker is not None
        assert worker._consumer_task is not None
        assert worker._drain_task is not None
        assert not worker._consumer_task.done()
        assert not worker._drain_task.done()

    async def test_stop_cleans_up(self, memory: CognitiveMemory) -> None:
        await memory.start()
        await memory.stop()

        assert memory._started is False
        assert memory._enrich_worker is None
        assert memory._stats_store._opened is False

    async def test_stop_is_idempotent(self, memory: CognitiveMemory) -> None:
        await memory.start()
        await memory.stop()
        await memory.stop()  # second stop is a no-op
        assert memory._started is False
        assert memory._enrich_worker is None

    async def test_stop_without_start_is_idempotent(self, memory: CognitiveMemory) -> None:
        """``stop`` on a never-started Module does not raise."""
        await memory.stop()
        assert memory._started is False
        assert memory._enrich_worker is None

    async def test_start_twice_raises(self, memory: CognitiveMemory) -> None:
        await memory.start()
        with pytest.raises(RuntimeError, match="called twice"):
            await memory.start()

    async def test_restart_after_stop(self, memory: CognitiveMemory) -> None:
        """A fresh ``start`` after ``stop`` re-opens the store and worker."""
        await memory.start()
        await memory.stop()

        await memory.start()
        assert memory._started is True
        assert memory._stats_store._opened is True
        assert memory._enrich_worker is not None


class TestLcmaDisabled:
    """When ``config.lcma.enabled`` is False, no EnrichWorker is constructed."""

    async def test_start_skips_worker_when_lcma_disabled(
        self,
        test_config: LithosConfig,
        mock_knowledge: MagicMock,
        mock_search: MagicMock,
        mock_graph: MagicMock,
        projection: ProvenanceProjection,
        event_bus: EventBus,
        mock_coordination: AsyncMock,
    ) -> None:
        test_config.lcma.enabled = False
        cm = await CognitiveMemory.create(
            config=test_config,
            knowledge=mock_knowledge,
            search=mock_search,
            graph=mock_graph,
            projection=projection,
            event_bus=event_bus,
            coordination=mock_coordination,
        )
        try:
            await cm.start()
            assert cm._started is True
            assert cm._stats_store._opened is True
            assert cm._enrich_worker is None
        finally:
            await cm.stop()
            assert cm._stats_store._opened is False
