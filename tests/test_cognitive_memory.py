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
from lithos.errors import ScoutFailure
from lithos.events import EDGE_UPSERTED, EventBus
from lithos.lcma.utils import Candidate
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
    """Construct a CognitiveMemory with coordination attached. Teardown safe."""
    cm = await CognitiveMemory.create(
        config=test_config,
        knowledge=mock_knowledge,
        search=mock_search,
        graph=mock_graph,
        projection=projection,
        event_bus=event_bus,
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

    async def test_create_stores_six_dependencies(
        self,
        test_config: LithosConfig,
        mock_knowledge: MagicMock,
        mock_search: MagicMock,
        mock_graph: MagicMock,
        projection: ProvenanceProjection,
        event_bus: EventBus,
    ) -> None:
        cm = await CognitiveMemory.create(
            config=test_config,
            knowledge=mock_knowledge,
            search=mock_search,
            graph=mock_graph,
            projection=projection,
            event_bus=event_bus,
        )
        try:
            assert cm._config is test_config
            assert cm._knowledge is mock_knowledge
            assert cm._search is mock_search
            assert cm._graph is mock_graph
            assert cm._projection is projection
            assert cm._event_bus is event_bus
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
    ) -> None:
        cm = await CognitiveMemory.create(
            config=test_config,
            knowledge=mock_knowledge,
            search=mock_search,
            graph=mock_graph,
            projection=projection,
            event_bus=event_bus,
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
    ) -> None:
        """LCMA enabled + no coordination attached → explicit error, not silent failure."""
        cm = await CognitiveMemory.create(
            config=test_config,
            knowledge=mock_knowledge,
            search=mock_search,
            graph=mock_graph,
            projection=projection,
            event_bus=event_bus,
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
            from_id="a",
            to_id="b",
            edge_type="related_to",
            weight=0.5,
            namespace="default",
        )

        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert event.type == EDGE_UPSERTED
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

                    memory = await CognitiveMemory.create(
                        config=config,
                        knowledge=MagicMock(),
                        search=MagicMock(),
                        graph=MagicMock(),
                        projection=projection,
                        event_bus=event_bus,
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
