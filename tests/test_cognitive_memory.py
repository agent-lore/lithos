"""Tests for the CognitiveMemory Module (ADR-0005).

Issue #255 covered construction and start/stop. Issue #257 adds the
``retrieve`` seam tests; issue #258 adds tests for the migrated public
methods ``node_stats`` and ``edge_upsert / edge_list / edge_delete``.
Remaining methods (``reinforce_*``, ``cache_lookup``, ``conflict_resolve``)
migrate in sibling slices (#259, #260) with their tests.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from lithos.cognitive_memory import CognitiveMemory, NodeStats
from lithos.config import LithosConfig
from lithos.errors import ScoutFailure, SearchBackendError
from lithos.events import EDGE_UPSERTED, EventBus
from lithos.intake import CorpusIntake
from lithos.knowledge import KnowledgeManager
from lithos.lcma.utils import Candidate
from lithos.provenance import ProvenanceProjection
from lithos.search import SearchEngine


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


@pytest.fixture
def intake(
    mock_knowledge: MagicMock,
    mock_search: MagicMock,
    mock_graph: MagicMock,
    event_bus: EventBus,
    mock_coordination: AsyncMock,
    projection: ProvenanceProjection,
) -> CorpusIntake:
    """A real CorpusIntake sharing the projection's underlying EdgeStore.

    ADR-0006 Slice 1 (#263) wires ``CognitiveMemory.edge_upsert`` through
    ``intake.assert_edge``. Sharing the edge store with ``projection`` keeps
    ``edge_list`` round-trips honest in the test.
    """
    return CorpusIntake(
        knowledge=mock_knowledge,
        search=mock_search,
        graph=mock_graph,
        coordination=mock_coordination,
        event_bus=event_bus,
        edge_store=projection._edge_store,
    )


@pytest_asyncio.fixture
async def memory(
    test_config: LithosConfig,
    mock_knowledge: MagicMock,
    mock_search: MagicMock,
    mock_graph: MagicMock,
    projection: ProvenanceProjection,
    event_bus: EventBus,
    mock_coordination: AsyncMock,
    intake: CorpusIntake,
):
    """Construct a CognitiveMemory with coordination attached. Teardown safe."""
    cm = await CognitiveMemory.create(
        config=test_config,
        knowledge=mock_knowledge,
        search=mock_search,
        graph=mock_graph,
        projection=projection,
        event_bus=event_bus,
        intake=intake,
    )
    cm.attach_coordination(mock_coordination)
    try:
        yield cm
    finally:
        await cm.stop()


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestCreate:
    """``CognitiveMemory.create`` wires the six-arg seam without opening stores."""

    async def test_create_constructs_module(self, memory: CognitiveMemory) -> None:
        assert memory._stats_store is not None
        # ``open()`` is deferred to ``start()`` — fresh store reports closed.
        assert memory._stats_store._opened is False
        assert memory._enrich_worker is None
        assert memory._started is False

    async def test_create_stores_seven_dependencies(
        self,
        test_config: LithosConfig,
        mock_knowledge: MagicMock,
        mock_search: MagicMock,
        mock_graph: MagicMock,
        projection: ProvenanceProjection,
        event_bus: EventBus,
        intake: CorpusIntake,
    ) -> None:
        cm = await CognitiveMemory.create(
            config=test_config,
            knowledge=mock_knowledge,
            search=mock_search,
            graph=mock_graph,
            projection=projection,
            event_bus=event_bus,
            intake=intake,
        )
        try:
            assert cm._config is test_config
            assert cm._knowledge is mock_knowledge
            assert cm._search is mock_search
            assert cm._graph is mock_graph
            assert cm._projection is projection
            assert cm._event_bus is event_bus
            assert cm._intake is intake
            # Coordination is NOT a constructor dep — set later via attach.
            assert cm._coordination is None
        finally:
            await cm.stop()

    async def test_attach_coordination_sets_dep(
        self,
        test_config: LithosConfig,
        mock_knowledge: MagicMock,
        mock_search: MagicMock,
        mock_graph: MagicMock,
        projection: ProvenanceProjection,
        event_bus: EventBus,
        mock_coordination: AsyncMock,
        intake: CorpusIntake,
    ) -> None:
        cm = await CognitiveMemory.create(
            config=test_config,
            knowledge=mock_knowledge,
            search=mock_search,
            graph=mock_graph,
            projection=projection,
            event_bus=event_bus,
            intake=intake,
        )
        try:
            cm.attach_coordination(mock_coordination)
            assert cm._coordination is mock_coordination
        finally:
            await cm.stop()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    """``start`` opens the StatsStore and starts the worker; ``stop`` reverses."""

    async def test_start_opens_stats_store_and_starts_worker(self, memory: CognitiveMemory) -> None:
        # Pre-condition: store is closed before start().
        assert memory._stats_store._opened is False

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

    async def test_start_without_attach_coordination_raises_when_lcma_enabled(
        self,
        test_config: LithosConfig,
        mock_knowledge: MagicMock,
        mock_search: MagicMock,
        mock_graph: MagicMock,
        projection: ProvenanceProjection,
        event_bus: EventBus,
        intake: CorpusIntake,
    ) -> None:
        """LCMA enabled + no coordination attached → explicit error, not silent failure."""
        cm = await CognitiveMemory.create(
            config=test_config,
            knowledge=mock_knowledge,
            search=mock_search,
            graph=mock_graph,
            projection=projection,
            event_bus=event_bus,
            intake=intake,
        )
        try:
            with pytest.raises(RuntimeError, match="coordination not attached"):
                await cm.start()
        finally:
            await cm.stop()


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
        intake: CorpusIntake,
    ) -> None:
        """No coordination attach needed when LCMA is disabled."""
        test_config.lcma.enabled = False
        cm = await CognitiveMemory.create(
            config=test_config,
            knowledge=mock_knowledge,
            search=mock_search,
            graph=mock_graph,
            projection=projection,
            event_bus=event_bus,
            intake=intake,
        )
        try:
            await cm.start()
            assert cm._started is True
            assert cm._stats_store._opened is True
            assert cm._enrich_worker is None
        finally:
            await cm.stop()
            assert cm._stats_store._opened is False


# ---------------------------------------------------------------------------
# Retrieve (issue #257)
# ---------------------------------------------------------------------------


class TestRetrieve:
    """``CognitiveMemory.retrieve`` is the public seam for the LCMA pipeline.

    These tests exercise the seam in isolation — the ``_run_retrieve_impl``
    function itself is covered exhaustively by ``tests/test_retrieve.py``.
    Here we only verify (1) the precondition contract, (2) that
    ``ScoutFailure`` is raised by the implementation but caught at the
    documented boundary, (3) that filter kwargs flow through, and (4) that
    the limit is honoured.
    """

    async def test_retrieve_before_start_raises(self, memory: CognitiveMemory) -> None:
        """Calling retrieve before start() is a programmer error."""
        with pytest.raises(RuntimeError, match="called before start"):
            await memory.retrieve("alpha", limit=3)

    async def test_retrieve_happy_path(self, memory: CognitiveMemory) -> None:
        """retrieve delegates to _run_retrieve_impl with the wired stores."""
        await memory.start()

        envelope: dict[str, object] = {
            "results": [],
            "temperature": 0.5,
            "terrace_reached": 1,
            "receipt_id": "test-receipt",
        }
        with patch(
            "lithos.lcma.retrieve._run_retrieve_impl",
            new_callable=AsyncMock,
            return_value=envelope,
        ) as impl:
            result = await memory.retrieve("alpha", limit=3, namespace_filter=["proj-a"])

        assert result is envelope
        impl.assert_awaited_once()
        kwargs = impl.await_args.kwargs
        assert kwargs["query"] == "alpha"
        assert kwargs["limit"] == 3
        assert kwargs["namespace_filter"] == ["proj-a"]
        # Stores wired from self
        assert kwargs["search"] is memory._search
        assert kwargs["knowledge"] is memory._knowledge
        assert kwargs["graph"] is memory._graph
        assert kwargs["coordination"] is memory._coordination
        assert kwargs["projection"] is memory._projection
        assert kwargs["stats_store"] is memory._stats_store
        assert kwargs["edge_store"] is memory._projection._edge_store
        assert kwargs["lcma_config"] is memory._config.lcma

    async def test_retrieve_scout_failure_logged_not_raised(
        self,
        memory: CognitiveMemory,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A backend that raises is wrapped in ScoutFailure, logged, and the
        retrieve completes successfully without that scout's results.

        ADR-0005 boundary contract: per-scout failures are caught inside
        the orchestrator (``_run_retrieve_impl``), not propagated to
        ``CognitiveMemory.retrieve``'s caller.
        """
        await memory.start()

        # Make every scout return [] except scout_vector which raises. The
        # scouts are imported into ``lithos.lcma.retrieve`` at module load,
        # so we patch them in *that* namespace, not in lithos.lcma.scouts.
        empty_scout = AsyncMock(return_value=[])
        # Stub the projection's edge_store.compute_temperature to a no-op
        # so we don't hit the real backend either.

        with (
            patch(
                "lithos.lcma.retrieve.scout_vector",
                new=AsyncMock(side_effect=RuntimeError("backend down")),
            ),
            patch("lithos.lcma.retrieve.scout_lexical", new=empty_scout),
            patch("lithos.lcma.retrieve.scout_exact_alias", new=empty_scout),
            patch("lithos.lcma.retrieve.scout_tags_recency", new=empty_scout),
            patch("lithos.lcma.retrieve.scout_freshness", new=empty_scout),
            patch("lithos.lcma.retrieve.scout_provenance", new=empty_scout),
            patch("lithos.lcma.retrieve.scout_graph", new=empty_scout),
            patch("lithos.lcma.retrieve.scout_coactivation", new=empty_scout),
            patch("lithos.lcma.retrieve.scout_source_url", new=empty_scout),
            caplog.at_level(logging.WARNING, logger="lithos.lcma.retrieve"),
        ):
            result = await memory.retrieve("alpha", limit=5)

        # Pipeline returned normally.
        assert result["receipt_id"]
        assert result["results"] == []

        # The phase A scout warning is present; the captured exc chain is a
        # ScoutFailure naming the failed scout.
        scout_records = [r for r in caplog.records if "phase A scout failed" in r.getMessage()]
        assert scout_records, "expected a phase A scout failure log record"
        record = scout_records[0]
        assert record.exc_info is not None
        exc = record.exc_info[1]
        assert isinstance(exc, ScoutFailure)
        assert exc.scout == "scout_vector"
        assert isinstance(exc.cause, RuntimeError)

    async def test_retrieve_namespace_filter_passes_through(self, memory: CognitiveMemory) -> None:
        """Cross-scout filter contract: every scout receives namespace_filter.

        See ``retrieve.py`` Phase A scout_kw construction — all scouts must
        enforce the same caller-supplied filters so the global view is
        consistent regardless of which backend a candidate came from.
        """
        await memory.start()

        vector_spy = AsyncMock(return_value=[])
        lexical_spy = AsyncMock(return_value=[])
        empty_scout = AsyncMock(return_value=[])

        with (
            patch("lithos.lcma.retrieve.scout_vector", new=vector_spy),
            patch("lithos.lcma.retrieve.scout_lexical", new=lexical_spy),
            patch("lithos.lcma.retrieve.scout_exact_alias", new=empty_scout),
            patch("lithos.lcma.retrieve.scout_tags_recency", new=empty_scout),
            patch("lithos.lcma.retrieve.scout_freshness", new=empty_scout),
        ):
            await memory.retrieve(
                "alpha",
                limit=5,
                namespace_filter=["proj-a"],
                tags=["t1"],
                path_prefix="docs/",
            )

        for spy in (vector_spy, lexical_spy):
            spy.assert_awaited_once()
            kwargs = spy.await_args.kwargs
            assert kwargs["namespace_filter"] == ["proj-a"]
            assert kwargs["tags"] == ["t1"]
            assert kwargs["path_prefix"] == "docs/"

    async def test_retrieve_limit_truncates_results(self, memory: CognitiveMemory) -> None:
        """The pipeline applies ``limit`` to the post-rerank candidate list."""
        await memory.start()

        # Build 25 distinct candidates so merge keeps them all (unique node_ids).
        candidates = [
            Candidate(
                node_id=f"node-{i:02d}",
                score=1.0 - i * 0.01,
                reasons=[f"reason-{i}"],
                scouts=["scout_vector"],
            )
            for i in range(25)
        ]
        empty_scout = AsyncMock(return_value=[])

        # ``knowledge.read`` is called for each result by the result-build
        # loop. With the fixture's MagicMock, default return is a MagicMock,
        # which the loop will treat as a present document. To keep the test
        # focused on truncation we instead patch knowledge.read to raise
        # FileNotFoundError, which the pipeline gracefully skips. That makes
        # the assertion ``len(results) <= limit`` the strongest guarantee
        # we can make without rebuilding the entire knowledge fixture.
        memory._knowledge.read = AsyncMock(side_effect=FileNotFoundError())
        memory._knowledge.get_cached_meta = MagicMock(return_value=None)

        with (
            patch(
                "lithos.lcma.retrieve.scout_vector",
                new=AsyncMock(return_value=candidates),
            ),
            patch("lithos.lcma.retrieve.scout_lexical", new=empty_scout),
            patch("lithos.lcma.retrieve.scout_exact_alias", new=empty_scout),
            patch("lithos.lcma.retrieve.scout_tags_recency", new=empty_scout),
            patch("lithos.lcma.retrieve.scout_freshness", new=empty_scout),
            patch("lithos.lcma.retrieve.scout_provenance", new=empty_scout),
            patch("lithos.lcma.retrieve.scout_graph", new=empty_scout),
            patch("lithos.lcma.retrieve.scout_coactivation", new=empty_scout),
            patch("lithos.lcma.retrieve.scout_source_url", new=empty_scout),
        ):
            result = await memory.retrieve("alpha", limit=5)

        results = result["results"]
        assert isinstance(results, list)
        assert len(results) <= 5


# ---------------------------------------------------------------------------
# Edges + node stats (issue #258)
# ---------------------------------------------------------------------------


class TestNodeStats:
    """``CognitiveMemory.node_stats`` returns NodeStats or the legacy error envelope."""

    async def test_returns_defaults_for_known_node_with_no_row(
        self, memory: CognitiveMemory, mock_knowledge: MagicMock
    ) -> None:
        # ``get_cached_meta`` only needs to be non-None for the node-exists check.
        mock_knowledge.get_cached_meta.return_value = MagicMock()
        await memory.start()

        result = await memory.node_stats("node-xyz")

        assert isinstance(result, NodeStats)
        assert result.node_id == "node-xyz"
        assert result.salience == 0.5
        assert result.retrieval_count == 0
        assert result.cited_count == 0
        assert result.last_retrieved_at is None
        assert result.last_used_at is None
        assert result.ignored_count == 0
        assert result.misleading_count == 0
        assert result.decay_rate == 0.0
        assert result.spaced_rep_strength == 0.0
        assert result.last_decay_applied_at is None

    async def test_returns_error_envelope_for_unknown_node(
        self, memory: CognitiveMemory, mock_knowledge: MagicMock
    ) -> None:
        mock_knowledge.get_cached_meta.return_value = None
        await memory.start()

        result = await memory.node_stats("missing")

        assert isinstance(result, dict)
        assert result["status"] == "error"
        assert result["code"] == "doc_not_found"
        assert "missing" in result["message"]

    async def test_returns_persisted_row_after_increment(
        self, memory: CognitiveMemory, mock_knowledge: MagicMock
    ) -> None:
        mock_knowledge.get_cached_meta.return_value = MagicMock()
        await memory.start()

        await memory._stats_store.increment_node_stats(node_id="node-1")
        result = await memory.node_stats("node-1")

        assert isinstance(result, NodeStats)
        assert result.node_id == "node-1"
        assert result.retrieval_count == 1
        # ``increment_node_stats`` initialises salience at 0.5 on first touch.
        assert result.salience == 0.5
        assert result.last_retrieved_at is not None


class TestEdgeMethods:
    """``CognitiveMemory.edge_upsert / edge_list / edge_delete`` round-trips."""

    async def test_edge_upsert_then_list_round_trip(self, memory: CognitiveMemory) -> None:
        await memory.start()

        edge_id = await memory.edge_upsert(
            agent="agent-1",
            from_id="a",
            to_id="b",
            edge_type="related_to",
            weight=0.7,
            namespace="default",
        )
        assert edge_id.startswith("edge_")

        edges = await memory.edge_list(from_id="a")
        assert len(edges) == 1
        assert edges[0]["edge_id"] == edge_id
        assert edges[0]["to_id"] == "b"
        assert edges[0]["type"] == "related_to"
        assert edges[0]["weight"] == 0.7

    async def test_edge_list_reads_through_projection(
        self, memory: CognitiveMemory, projection: ProvenanceProjection
    ) -> None:
        """edge_list MUST hit ProvenanceProjection.list_edges, not EdgeStore directly.

        Seed via the projection's internal edge store (the same path
        ``edge_upsert`` uses today) and assert ``edge_list`` surfaces the
        row — proving the read goes through the projection's API.
        """
        await memory.start()
        await projection._edge_store.upsert(
            from_id="x",
            to_id="y",
            edge_type="derived_from",
            weight=1.0,
            namespace="default",
            provenance_type="frontmatter",
        )

        edges = await memory.edge_list(edge_type="derived_from")

        assert any(e["from_id"] == "x" and e["to_id"] == "y" for e in edges)

    async def test_edge_delete_removes_edge(self, memory: CognitiveMemory) -> None:
        await memory.start()
        edge_id = await memory.edge_upsert(
            agent="agent-1",
            from_id="a",
            to_id="b",
            edge_type="related_to",
            weight=0.5,
            namespace="default",
        )

        deleted = await memory.edge_delete(edge_ids=[edge_id])

        assert deleted == 1
        assert await memory.edge_list(from_id="a") == []

    async def test_edge_delete_with_empty_list_is_noop(self, memory: CognitiveMemory) -> None:
        await memory.start()
        assert await memory.edge_delete(edge_ids=[]) == 0

    async def test_edge_upsert_emits_edge_upserted_event(
        self, memory: CognitiveMemory, event_bus: EventBus
    ) -> None:
        queue = event_bus.subscribe(event_types=[EDGE_UPSERTED])
        await memory.start()

        edge_id = await memory.edge_upsert(
            agent="agent-1",
            from_id="a",
            to_id="b",
            edge_type="related_to",
            weight=0.5,
            namespace="default",
        )

        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert event.type == EDGE_UPSERTED
        assert event.agent == "agent-1"
        assert event.payload["edge_id"] == edge_id
        assert event.payload["from_id"] == "a"
        assert event.payload["to_id"] == "b"
        assert event.payload["type"] == "related_to"
        assert event.payload["namespace"] == "default"


# ---------------------------------------------------------------------------
# Process-lifecycle validation (no-thread-leak guarantee)
# ---------------------------------------------------------------------------


class TestProcessLifecycleValidation:
    """Subprocess regression guard for the 'starts/stops without leaking threads'
    acceptance criterion from issue #255. Mirrors
    ``tests/test_server_lifecycle.py::TestServerLifecycleValidation``: a hung
    thread or unclosed aiosqlite worker prevents the interpreter from exiting,
    and this test fails on that exact symptom (subprocess timeout).
    """

    def test_create_start_stop_exits_cleanly_in_subprocess(self, temp_dir: Path) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        script = textwrap.dedent(
            f"""
            import asyncio
            from pathlib import Path
            from unittest.mock import AsyncMock, MagicMock

            from lithos.cognitive_memory import CognitiveMemory
            from lithos.config import LithosConfig, StorageConfig
            from lithos.events import EventBus
            from lithos.intake import CorpusIntake
            from lithos.provenance import ProvenanceProjection


            async def main() -> None:
                root = Path({str(temp_dir)!r})
                for index in range(3):
                    config = LithosConfig(
                        storage=StorageConfig(data_dir=root / f"cm-run-{{index}}")
                    )
                    config.ensure_directories()

                    projection = await ProvenanceProjection.create(config)
                    event_bus = EventBus(config.events)

                    intake = CorpusIntake(
                        knowledge=MagicMock(),
                        search=MagicMock(),
                        graph=MagicMock(),
                        coordination=AsyncMock(),
                        event_bus=event_bus,
                        edge_store=projection._edge_store,
                    )

                    memory = await CognitiveMemory.create(
                        config=config,
                        knowledge=MagicMock(),
                        search=MagicMock(),
                        graph=MagicMock(),
                        projection=projection,
                        event_bus=event_bus,
                        intake=intake,
                    )
                    memory.attach_coordination(AsyncMock())

                    await memory.start()
                    assert memory._started is True
                    assert memory._stats_store._opened is True
                    assert memory._enrich_worker is not None

                    await memory.stop()
                    assert memory._started is False
                    assert memory._enrich_worker is None
                    assert memory._stats_store._opened is False

                    await projection.close()

                print("cm-lifecycle-ok")


            asyncio.run(main())
            """
        )
        env = os.environ.copy()
        src_path = str(repo_root / "src")
        existing_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            src_path
            if not existing_pythonpath
            else os.pathsep.join((src_path, existing_pythonpath))
        )

        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert "cm-lifecycle-ok" in result.stdout


# ---------------------------------------------------------------------------
# Reinforcement (issue #259)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def memory_with_knowledge(
    test_config: LithosConfig,
    knowledge_manager: KnowledgeManager,
    mock_search: MagicMock,
    mock_graph: MagicMock,
    projection: ProvenanceProjection,
    event_bus: EventBus,
    mock_coordination: AsyncMock,
):
    """CognitiveMemory wired with the real KnowledgeManager.

    The reinforcement methods read ``knowledge.get_cached_meta`` and call
    ``knowledge.update`` (for quarantine), so they need a real
    ``KnowledgeManager`` rather than the ``MagicMock`` used by the
    lifecycle tests above. ``start()`` is invoked here so the StatsStore
    is open for each test.
    """
    real_intake = CorpusIntake(
        knowledge=knowledge_manager,
        search=mock_search,
        graph=mock_graph,
        coordination=mock_coordination,
        event_bus=event_bus,
        edge_store=projection._edge_store,
    )
    cm = await CognitiveMemory.create(
        config=test_config,
        knowledge=knowledge_manager,
        search=mock_search,
        graph=mock_graph,
        projection=projection,
        event_bus=event_bus,
        intake=real_intake,
    )
    cm.attach_coordination(mock_coordination)
    await cm.start()
    try:
        yield cm
    finally:
        await cm.stop()


async def _create_note(
    km: KnowledgeManager,
    title: str,
    *,
    namespace: str | None = None,
) -> str:
    """Helper: create a note via the real KnowledgeManager and return its id."""
    result = await km.create(
        title=title,
        content=f"Content for {title}",
        agent="test-agent",
        namespace=namespace,
    )
    assert result.document is not None
    return result.document.id


class TestReinforceCited:
    """``reinforce_cited`` updates per-node stats in the owned StatsStore."""

    async def test_increments_cited_count(
        self,
        memory_with_knowledge: CognitiveMemory,
        knowledge_manager: KnowledgeManager,
    ) -> None:
        nid = await _create_note(knowledge_manager, "Note A")

        await memory_with_knowledge.reinforce_cited([nid])

        stats = await memory_with_knowledge._stats_store.get_node_stats(nid)
        assert stats is not None
        assert stats["cited_count"] == 1

    async def test_bumps_salience_by_0_02(
        self,
        memory_with_knowledge: CognitiveMemory,
        knowledge_manager: KnowledgeManager,
    ) -> None:
        nid = await _create_note(knowledge_manager, "Note B")

        await memory_with_knowledge.reinforce_cited([nid])

        stats = await memory_with_knowledge._stats_store.get_node_stats(nid)
        assert stats is not None
        # Default salience 0.5 + 0.02
        assert stats["salience"] == pytest.approx(0.52)

    async def test_bumps_spaced_rep_strength_by_0_05(
        self,
        memory_with_knowledge: CognitiveMemory,
        knowledge_manager: KnowledgeManager,
    ) -> None:
        nid = await _create_note(knowledge_manager, "Note C")

        await memory_with_knowledge.reinforce_cited([nid])

        stats = await memory_with_knowledge._stats_store.get_node_stats(nid)
        assert stats is not None
        assert stats["spaced_rep_strength"] == pytest.approx(0.05)

    async def test_accumulates_across_calls(
        self,
        memory_with_knowledge: CognitiveMemory,
        knowledge_manager: KnowledgeManager,
    ) -> None:
        nid = await _create_note(knowledge_manager, "Note D")

        await memory_with_knowledge.reinforce_cited([nid])
        await memory_with_knowledge.reinforce_cited([nid])

        stats = await memory_with_knowledge._stats_store.get_node_stats(nid)
        assert stats is not None
        assert stats["cited_count"] == 2
        assert stats["salience"] == pytest.approx(0.54)
        assert stats["spaced_rep_strength"] == pytest.approx(0.10)


class TestReinforceIgnored:
    """``reinforce_ignored`` increments count and only decays past threshold."""

    async def test_increments_ignored_count(
        self,
        memory_with_knowledge: CognitiveMemory,
        knowledge_manager: KnowledgeManager,
    ) -> None:
        nid = await _create_note(knowledge_manager, "Note Ign-A")

        await memory_with_knowledge.reinforce_ignored([nid])

        stats = await memory_with_knowledge._stats_store.get_node_stats(nid)
        assert stats is not None
        assert stats["ignored_count"] == 1

    async def test_does_not_decay_salience_under_threshold(
        self,
        memory_with_knowledge: CognitiveMemory,
        knowledge_manager: KnowledgeManager,
    ) -> None:
        """ignored_count <= 5 leaves salience at the default 0.5."""
        nid = await _create_note(knowledge_manager, "Note Ign-B")

        for _ in range(5):
            await memory_with_knowledge.reinforce_ignored([nid])

        stats = await memory_with_knowledge._stats_store.get_node_stats(nid)
        assert stats is not None
        assert stats["ignored_count"] == 5
        assert stats["salience"] == pytest.approx(0.5)

    async def test_decays_salience_when_chronic(
        self,
        memory_with_knowledge: CognitiveMemory,
        knowledge_manager: KnowledgeManager,
    ) -> None:
        """6th call (ignored=6, cited=0) triggers a -0.02 salience hit."""
        nid = await _create_note(knowledge_manager, "Note Ign-C")

        for _ in range(6):
            await memory_with_knowledge.reinforce_ignored([nid])

        stats = await memory_with_knowledge._stats_store.get_node_stats(nid)
        assert stats is not None
        assert stats["ignored_count"] == 6
        assert stats["salience"] == pytest.approx(0.5 - 0.02)


class TestReinforceBetween:
    """``reinforce_between`` writes related_to edges via the projection store."""

    async def test_creates_edge_for_new_pair(
        self,
        memory_with_knowledge: CognitiveMemory,
        knowledge_manager: KnowledgeManager,
    ) -> None:
        n1 = await _create_note(knowledge_manager, "Note Pair-A1")
        n2 = await _create_note(knowledge_manager, "Note Pair-A2")

        await memory_with_knowledge.reinforce_between([n1, n2])

        from_id, to_id = sorted([n1, n2])
        edges = await memory_with_knowledge._projection._edge_store.list_edges(
            from_id=from_id, to_id=to_id, edge_type="related_to"
        )
        assert len(edges) == 1
        assert edges[0]["weight"] == pytest.approx(0.5)
        assert edges[0]["provenance_type"] == "reinforcement"

    async def test_strengthens_existing_edge_by_0_03(
        self,
        memory_with_knowledge: CognitiveMemory,
        knowledge_manager: KnowledgeManager,
    ) -> None:
        n1 = await _create_note(knowledge_manager, "Note Pair-B1")
        n2 = await _create_note(knowledge_manager, "Note Pair-B2")

        # First call creates the edge at 0.5; second strengthens by +0.03.
        await memory_with_knowledge.reinforce_between([n1, n2])
        await memory_with_knowledge.reinforce_between([n1, n2])

        from_id, to_id = sorted([n1, n2])
        edges = await memory_with_knowledge._projection._edge_store.list_edges(
            from_id=from_id, to_id=to_id, edge_type="related_to"
        )
        assert len(edges) == 1
        assert edges[0]["weight"] == pytest.approx(0.53)

    async def test_skips_cross_namespace_pairs(
        self,
        memory_with_knowledge: CognitiveMemory,
        knowledge_manager: KnowledgeManager,
    ) -> None:
        n1 = await _create_note(knowledge_manager, "Note Cross-1", namespace="alpha")
        n2 = await _create_note(knowledge_manager, "Note Cross-2", namespace="beta")

        await memory_with_knowledge.reinforce_between([n1, n2])

        all_edges = await memory_with_knowledge._projection._edge_store.list_edges(
            edge_type="related_to"
        )
        assert len(all_edges) == 0

    async def test_canonicalises_pair_order(
        self,
        memory_with_knowledge: CognitiveMemory,
        knowledge_manager: KnowledgeManager,
    ) -> None:
        """Stored edges always have from_id <= to_id lexicographically."""
        n1 = await _create_note(knowledge_manager, "Note Order-1")
        n2 = await _create_note(knowledge_manager, "Note Order-2")

        await memory_with_knowledge.reinforce_between([n1, n2])

        edges = await memory_with_knowledge._projection._edge_store.list_edges(
            edge_type="related_to"
        )
        assert len(edges) == 1
        edge = edges[0]
        assert str(edge["from_id"]) <= str(edge["to_id"])


class TestReinforceMisleading:
    """``reinforce_misleading`` penalises stats, weakens edges, quarantines repeats."""

    async def test_increments_count_and_decays_salience(
        self,
        memory_with_knowledge: CognitiveMemory,
        knowledge_manager: KnowledgeManager,
    ) -> None:
        nid = await _create_note(knowledge_manager, "Note Mis-A")

        await memory_with_knowledge.reinforce_misleading([nid])

        stats = await memory_with_knowledge._stats_store.get_node_stats(nid)
        assert stats is not None
        assert stats["misleading_count"] == 1
        assert stats["salience"] == pytest.approx(0.5 - 0.05)

    async def test_quarantines_after_three_hits(
        self,
        memory_with_knowledge: CognitiveMemory,
        knowledge_manager: KnowledgeManager,
    ) -> None:
        nid = await _create_note(knowledge_manager, "Note Mis-B")

        for _ in range(3):
            await memory_with_knowledge.reinforce_misleading([nid])

        cached = knowledge_manager.get_cached_meta(nid)
        assert cached is not None
        assert cached.status == "quarantined"

    async def test_weakens_adjacent_edges_by_0_05(
        self,
        memory_with_knowledge: CognitiveMemory,
        knowledge_manager: KnowledgeManager,
    ) -> None:
        """Edges touching a misleading node are weakened by -0.05 exactly once."""
        n1 = await _create_note(knowledge_manager, "Note Mis-Edge-1")
        n2 = await _create_note(knowledge_manager, "Note Mis-Edge-2")

        # Seed a related_to edge between the two nodes at weight 0.5.
        await memory_with_knowledge.reinforce_between([n1, n2])

        # Both endpoints flagged misleading — edge weakened exactly once.
        await memory_with_knowledge.reinforce_misleading([n1, n2])

        from_id, to_id = sorted([n1, n2])
        edges = await memory_with_knowledge._projection._edge_store.list_edges(
            from_id=from_id, to_id=to_id, edge_type="related_to"
        )
        assert len(edges) == 1
        assert edges[0]["weight"] == pytest.approx(0.45)


# ---------------------------------------------------------------------------
# Cache + conflict resolve (issue #260)
# ---------------------------------------------------------------------------


class TestCacheLookup:
    """Validation paths and search-backend errors of ``cache_lookup``.

    Hit-path / staleness coverage stays in ``tests/test_server.py::TestCacheLookup``,
    which exercises the same code through the MCP wrapper.
    """

    async def test_clean_miss_returns_empty_envelope(self, memory: CognitiveMemory) -> None:
        memory._search.semantic_search = MagicMock(return_value=[])
        result = await memory.cache_lookup(query="nothing matches")
        assert result == {
            "hit": False,
            "document": None,
            "stale_exists": False,
            "stale_id": None,
        }

    async def test_invalid_max_age_hours_returns_error_envelope(
        self, memory: CognitiveMemory
    ) -> None:
        result = await memory.cache_lookup(query="x", max_age_hours=-1)
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"
        assert "max_age_hours" in result["message"]

    async def test_invalid_limit_returns_error_envelope(self, memory: CognitiveMemory) -> None:
        result = await memory.cache_lookup(query="x", limit=0)
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"

    async def test_invalid_min_confidence_returns_error_envelope(
        self, memory: CognitiveMemory
    ) -> None:
        result = await memory.cache_lookup(query="x", min_confidence=1.5)
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"

    async def test_search_backend_error_returns_error_envelope(
        self, memory: CognitiveMemory
    ) -> None:
        err = SearchBackendError("chroma down", {"chroma": RuntimeError("boom")})
        memory._search.semantic_search = MagicMock(side_effect=err)
        result = await memory.cache_lookup(query="x")
        assert result["status"] == "error"
        assert result["code"] == "search_backend_error"
        assert "chroma" in result["message"]


class TestCacheLookupHitPath:
    """Hit-path coverage that exercises real KnowledgeManager + SearchEngine.

    Uses a dedicated fixture instead of the ``memory`` fixture (which mocks
    the knowledge / search collaborators) so the document round-trip is
    real and the ``meta.is_stale`` / ``confidence`` filters fire on actual
    frontmatter rather than mock returns.
    """

    @pytest_asyncio.fixture
    async def real_memory(
        self,
        test_config: LithosConfig,
        projection: ProvenanceProjection,
        event_bus: EventBus,
        mock_coordination: AsyncMock,
    ):
        knowledge = KnowledgeManager(test_config)
        search = await SearchEngine.create(test_config)
        graph_stub = MagicMock()
        real_intake = CorpusIntake(
            knowledge=knowledge,
            search=search,
            graph=graph_stub,
            coordination=mock_coordination,
            event_bus=event_bus,
            edge_store=projection._edge_store,
        )
        cm = await CognitiveMemory.create(
            config=test_config,
            knowledge=knowledge,
            search=search,
            graph=graph_stub,
            projection=projection,
            event_bus=event_bus,
            intake=real_intake,
        )
        cm.attach_coordination(mock_coordination)
        try:
            yield cm
        finally:
            await cm.stop()

    async def test_hit_returns_document_envelope(self, real_memory: CognitiveMemory) -> None:
        from datetime import datetime, timedelta, timezone

        doc = (
            await real_memory._knowledge.create(
                title="Quantum Computing Notes",
                content="Information about quantum computing.",
                agent="agent",
                tags=["research"],
                expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            )
        ).document
        real_memory._search.index(KnowledgeManager.to_indexable(doc))

        result = await real_memory.cache_lookup(query="quantum computing", tags=["research"])
        assert result["hit"] is True
        assert result["document"]["id"] == doc.id

    async def _write_poisoned_doc(
        self,
        real_memory: CognitiveMemory,
        test_config: LithosConfig,
        *,
        confidence_yaml: str,
    ) -> None:
        """Write a raw .md with a poisoned confidence value, then load + index it.

        Simulates a legacy doc persisted before write-side validation existed
        (#312) — write validation cannot prevent these, so reads must heal them.
        """
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        raw = textwrap.dedent(f"""\
            ---
            id: poisoned-doc
            title: Quantum Computing Notes
            author: lithos-enrich
            created_at: '{now}'
            updated_at: '{now}'
            tags: [research]
            confidence: {confidence_yaml}
            ---

            # Quantum Computing Notes

            Information about quantum computing.
            """)
        path = Path("poisoned-doc.md")
        (test_config.storage.knowledge_path / path).write_text(raw)
        doc = await real_memory._knowledge.sync_from_disk(path)
        real_memory._search.index(KnowledgeManager.to_indexable(doc))

    async def test_null_confidence_frontmatter_does_not_crash_lookup(
        self, real_memory: CognitiveMemory, test_config: LithosConfig
    ) -> None:
        """Regression for #312: a candidate doc with ``confidence: null`` must not
        abort the lookup with TypeError; it heals to the default 1.0."""
        await self._write_poisoned_doc(real_memory, test_config, confidence_yaml="null")

        result = await real_memory.cache_lookup(query="quantum computing")
        assert result["hit"] is True
        assert result["document"]["confidence"] == 1.0

    async def test_string_confidence_frontmatter_does_not_crash_lookup(
        self, real_memory: CognitiveMemory, test_config: LithosConfig
    ) -> None:
        """Regression for #312: a non-numeric string confidence heals to 1.0."""
        await self._write_poisoned_doc(real_memory, test_config, confidence_yaml="medium")

        result = await real_memory.cache_lookup(query="quantum computing")
        assert result["hit"] is True
        assert result["document"]["confidence"] == 1.0


class TestConflictResolve:
    """Routing and persistence checks for the migrated ``conflict_resolve``."""

    async def _make_contradiction_edge(
        self, memory: CognitiveMemory, *, from_id: str = "note-a", to_id: str = "note-b"
    ) -> str:
        return await memory._projection._edge_store.upsert(
            from_id=from_id,
            to_id=to_id,
            edge_type="contradicts",
            weight=1.0,
            namespace="default",
        )

    async def test_happy_path_persists_and_returns_ok(self, memory: CognitiveMemory) -> None:
        edge_id = await self._make_contradiction_edge(memory)

        result = await memory.conflict_resolve(
            edge_id=edge_id,
            resolution="accepted_dual",
            resolver="agent-x",
        )
        assert result == {
            "status": "ok",
            "edge_id": edge_id,
            "conflict_state": "accepted_dual",
        }

        roundtrip = await memory._projection.get_edge(edge_id)
        assert roundtrip is not None
        assert roundtrip["conflict_state"] == "accepted_dual"
        assert roundtrip["provenance_actor"] == "agent-x"

    async def test_invalid_resolution_rejected(self, memory: CognitiveMemory) -> None:
        edge_id = await self._make_contradiction_edge(memory)
        result = await memory.conflict_resolve(
            edge_id=edge_id,
            resolution="bogus",
            resolver="agent-x",
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"

    async def test_unknown_edge_returns_not_found(self, memory: CognitiveMemory) -> None:
        result = await memory.conflict_resolve(
            edge_id="00000000-0000-0000-0000-000000000000",
            resolution="accepted_dual",
            resolver="agent-x",
        )
        assert result["status"] == "error"
        assert result["code"] == "not_found"

    async def test_non_contradicts_edge_rejected(self, memory: CognitiveMemory) -> None:
        edge_id = await memory._projection._edge_store.upsert(
            from_id="note-a",
            to_id="note-b",
            edge_type="related_to",
            weight=0.5,
            namespace="default",
        )
        result = await memory.conflict_resolve(
            edge_id=edge_id,
            resolution="accepted_dual",
            resolver="agent-x",
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"
        assert "not 'contradicts'" in result["message"]

    async def test_superseded_requires_winner_id(self, memory: CognitiveMemory) -> None:
        edge_id = await self._make_contradiction_edge(memory)
        result = await memory.conflict_resolve(
            edge_id=edge_id,
            resolution="superseded",
            resolver="agent-x",
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"
        assert "winner_id" in result["message"]

    async def test_superseded_winner_must_be_endpoint(self, memory: CognitiveMemory) -> None:
        edge_id = await self._make_contradiction_edge(memory)
        result = await memory.conflict_resolve(
            edge_id=edge_id,
            resolution="superseded",
            resolver="agent-x",
            winner_id="note-c",
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"
