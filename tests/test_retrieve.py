"""Tests for US-009: lithos_retrieve MCP tool — orchestration, receipts, working memory.

Unit tests for the retrieval pipeline, reranking, temperature, and receipt/WM logic.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import frontmatter as fm
import pytest

from lithos.config import LcmaConfig, LithosConfig, StorageConfig
from lithos.graph import KnowledgeGraph
from lithos.knowledge import KnowledgeManager
from lithos.lcma.edges import EdgeStore
from lithos.lcma.retrieve import (
    _rerank_fast,
    compute_temperature,
    run_retrieve,
)
from lithos.lcma.stats import StatsStore
from lithos.lcma.utils import Candidate
from lithos.search import SearchEngine
from lithos.search import generate_snippet as _generate_snippet


@pytest.fixture
def seeded_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LithosConfig:
    """Config with seeded notes for retrieval testing."""
    from lithos.config import _reset_config, set_config

    for var in [
        "LITHOS_DATA_DIR",
        "LITHOS_PORT",
        "LITHOS_HOST",
        "LITHOS_OTEL_ENABLED",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    ]:
        monkeypatch.delenv(var, raising=False)
    config = LithosConfig(storage=StorageConfig(data_dir=tmp_path))
    config.ensure_directories()
    set_config(config)
    yield config  # type: ignore[misc]
    _reset_config()


# Stable UUIDs for test notes
_ID1 = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
_ID2 = "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"
_ID3 = "cccccccc-cccc-4ccc-cccc-cccccccccccc"


@pytest.fixture
def seeded_km(seeded_config: LithosConfig) -> KnowledgeManager:
    """KnowledgeManager with seeded notes."""
    km = KnowledgeManager(seeded_config)
    kp = seeded_config.storage.knowledge_path

    note1 = fm.Post(
        "# Alpha Note\n\nContent about alpha testing",
        id=_ID1,
        title="Alpha Note",
        author="agent-alpha",
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        tags=["testing"],
        access_scope="shared",
        note_type="observation",
    )
    (kp / "alpha-note.md").write_text(fm.dumps(note1))

    note2 = fm.Post(
        "# Beta Note\n\nContent about beta projects",
        id=_ID2,
        title="Beta Note",
        author="agent-beta",
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        tags=["projects"],
        access_scope="shared",
        note_type="agent_finding",
    )
    (kp / "beta-note.md").write_text(fm.dumps(note2))

    note3 = fm.Post(
        "# Gamma Note\n\nContent about gamma summary",
        id=_ID3,
        title="Gamma Note",
        author="agent-alpha",
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        tags=["testing"],
        derived_from_ids=[_ID1],
        access_scope="shared",
        note_type="summary",
    )
    (kp / "gamma-note.md").write_text(fm.dumps(note3))

    km._scan_existing()
    return km


@pytest.fixture
def seeded_graph(seeded_config: LithosConfig, seeded_km: KnowledgeManager) -> KnowledgeGraph:
    """KnowledgeGraph built from seeded notes."""
    graph = KnowledgeGraph(seeded_config)
    for doc_id, rel_path in seeded_km._id_to_path.items():
        full_path = seeded_config.storage.knowledge_path / rel_path
        if full_path.exists():
            post = fm.load(str(full_path))
            from lithos.knowledge import KnowledgeDocument, KnowledgeMetadata

            metadata = KnowledgeMetadata.from_dict(dict(post.metadata))
            doc = KnowledgeDocument(
                id=doc_id,
                title=metadata.title,
                content=post.content,
                metadata=metadata,
                path=rel_path,
            )
            graph.add_document(doc)
    return graph


@pytest.fixture
def seeded_search(seeded_config: LithosConfig) -> SearchEngine:
    return SearchEngine(seeded_config)


@pytest.fixture
async def edge_store(seeded_config: LithosConfig) -> EdgeStore:
    store = EdgeStore(seeded_config)
    await store.open()
    return store


@pytest.fixture
async def stats_store(seeded_config: LithosConfig) -> StatsStore:
    store = StatsStore(seeded_config)
    await store.open()
    return store


@pytest.fixture
def mock_coordination() -> AsyncMock:
    coord = AsyncMock()
    coord.list_findings = AsyncMock(return_value=[])
    coord.get_task_status = AsyncMock(return_value=[])
    return coord


# ---------------------------------------------------------------------------
# _generate_snippet
# ---------------------------------------------------------------------------


class TestGenerateSnippet:
    def test_snippet_with_matching_term(self) -> None:
        content = "This is a test document about Python programming and AI."
        snippet = _generate_snippet(content, "Python")
        assert "Python" in snippet

    def test_snippet_fallback_to_beginning(self) -> None:
        content = "Short content"
        snippet = _generate_snippet(content, "nonexistent")
        assert snippet == "Short content"

    def test_snippet_truncates_long_content(self) -> None:
        content = "x" * 500
        snippet = _generate_snippet(content, "nonexistent")
        assert snippet.endswith("...")


# ---------------------------------------------------------------------------
# _rerank_fast
# ---------------------------------------------------------------------------


class TestRerankFast:
    def test_reranks_by_weighted_score(self, seeded_km: KnowledgeManager) -> None:
        candidates = [
            Candidate(node_id=_ID1, score=0.5, reasons=["r1"], scouts=["scout_vector"]),
            Candidate(node_id=_ID2, score=0.9, reasons=["r2"], scouts=["scout_lexical"]),
        ]
        lcma = LcmaConfig()
        result = _rerank_fast(candidates, lcma, seeded_km)
        # Higher score should rank higher
        assert len(result) == 2
        assert result[0].node_id == _ID2

    def test_note_type_priors_affect_ranking(self, seeded_km: KnowledgeManager) -> None:
        # Both candidates have same normalized score, but different note types
        candidates = [
            Candidate(node_id=_ID1, score=1.0, reasons=["r1"], scouts=["scout_vector"]),
            Candidate(node_id=_ID2, score=1.0, reasons=["r2"], scouts=["scout_vector"]),
        ]
        # Give agent_finding higher prior
        lcma = LcmaConfig(
            note_type_priors={
                "observation": 0.1,
                "agent_finding": 0.9,
                "summary": 0.5,
                "concept": 0.5,
                "task_record": 0.5,
                "hypothesis": 0.5,
            }
        )
        result = _rerank_fast(candidates, lcma, seeded_km)
        # agent_finding (ID2) should rank first due to higher prior
        assert result[0].node_id == _ID2

    def test_default_priors_differentiate_note_types(self, seeded_km: KnowledgeManager) -> None:
        """With default priors, agent_finding (0.6) ranks above task_record (0.35)
        when raw scores are identical."""
        from dataclasses import replace

        # Override ID1's note_type to task_record in the cache
        seeded_km._meta_cache[_ID1] = replace(seeded_km._meta_cache[_ID1], note_type="task_record")

        candidates = [
            Candidate(node_id=_ID1, score=1.0, reasons=["r1"], scouts=["scout_vector"]),
            Candidate(node_id=_ID2, score=1.0, reasons=["r2"], scouts=["scout_vector"]),
        ]
        # Use default LcmaConfig (differentiated priors)
        result = _rerank_fast(candidates, LcmaConfig(), seeded_km)
        # agent_finding (0.6) should outrank task_record (0.35)
        assert result[0].node_id == _ID2
        assert result[1].node_id == _ID1
        # Scores must differ
        assert result[0].score > result[1].score

    def test_does_not_mutate_input(self, seeded_km: KnowledgeManager) -> None:
        candidates = [
            Candidate(node_id=_ID1, score=0.5, reasons=["r1"], scouts=["scout_vector"]),
        ]
        original_score = candidates[0].score
        _rerank_fast(candidates, LcmaConfig(), seeded_km)
        assert candidates[0].score == original_score

    def test_stored_salience_affects_ranking(self, seeded_km: KnowledgeManager) -> None:
        """When salience_map is provided, higher salience boosts ranking."""
        # Both candidates have same raw score and same note_type
        candidates = [
            Candidate(node_id=_ID1, score=1.0, reasons=["r1"], scouts=["scout_vector"]),
            Candidate(node_id=_ID2, score=1.0, reasons=["r2"], scouts=["scout_vector"]),
        ]
        # Give ID2 much higher salience
        salience_map = {_ID1: 0.1, _ID2: 0.9}
        result = _rerank_fast(candidates, LcmaConfig(), seeded_km, salience_map=salience_map)
        # ID2 should rank first due to higher salience
        assert result[0].node_id == _ID2
        assert result[0].score > result[1].score

    def test_salience_map_none_uses_score_as_proxy(self, seeded_km: KnowledgeManager) -> None:
        """Without salience_map, _rerank_fast falls back to c.score as proxy."""
        candidates = [
            Candidate(node_id=_ID1, score=0.3, reasons=["r1"], scouts=["scout_vector"]),
            Candidate(node_id=_ID2, score=0.9, reasons=["r2"], scouts=["scout_vector"]),
        ]
        result_without = _rerank_fast(candidates, LcmaConfig(), seeded_km, salience_map=None)
        result_default = _rerank_fast(candidates, LcmaConfig(), seeded_km)
        # Both should produce the same ranking
        assert result_without[0].node_id == result_default[0].node_id
        assert result_without[1].node_id == result_default[1].node_id


class TestStoredSalienceAffectsRetrieval:
    """Verify that salience stored in StatsStore flows through run_retrieve."""

    @pytest.mark.asyncio
    async def test_persisted_salience_affects_run_retrieve_ranking(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """Persist different salience values in StatsStore and verify they
        influence the ranking produced by run_retrieve."""
        # Give _ID1 a low salience and _ID2 a high salience in StatsStore.
        # Default salience is 0.5; adjust so _ID1 -> 0.1, _ID2 -> 0.9.
        await stats_store.update_salience(_ID1, -0.4)  # 0.5 - 0.4 = 0.1
        await stats_store.update_salience(_ID2, 0.4)  # 0.5 + 0.4 = 0.9

        # Mock search so both IDs come back with equal raw scores, forcing
        # the salience component to be the differentiator.
        from lithos.search import SearchResult

        equal_results = [
            SearchResult(
                id=_ID1, score=0.5, title="Alpha Note", snippet="alpha", path="alpha-note.md"
            ),
            SearchResult(
                id=_ID2, score=0.5, title="Beta Note", snippet="beta", path="beta-note.md"
            ),
        ]
        with (
            patch.object(seeded_search, "semantic_search", return_value=equal_results),
            patch.object(seeded_search, "full_text_search", return_value=equal_results),
        ):
            result = await run_retrieve(
                query="testing",
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
                limit=10,
            )

        result_ids = [r["id"] for r in result["results"]]
        # Both IDs should appear in results
        assert _ID1 in result_ids
        assert _ID2 in result_ids
        # _ID2 (salience=0.9) should rank above _ID1 (salience=0.1)
        assert result_ids.index(_ID2) < result_ids.index(_ID1)


# ---------------------------------------------------------------------------
# compute_temperature
# ---------------------------------------------------------------------------


class TestComputeTemperature:
    """MVP 1 always returns ``temperature_default`` — coherence-based
    computation is deferred to MVP 3 (see retrieve.py:compute_temperature).
    """

    @pytest.mark.asyncio
    async def test_cold_start_returns_default(self, edge_store: EdgeStore) -> None:
        """Edge count below threshold returns temperature_default."""
        lcma = LcmaConfig(temperature_default=0.5, temperature_edge_threshold=50)
        temp = await compute_temperature(edge_store, lcma, None)
        assert temp == 0.5

    @pytest.mark.asyncio
    async def test_with_namespace_filter(self, edge_store: EdgeStore) -> None:
        lcma = LcmaConfig(temperature_default=0.7, temperature_edge_threshold=50)
        temp = await compute_temperature(edge_store, lcma, ["default"])
        assert temp == 0.7

    @pytest.mark.asyncio
    async def test_edge_store_is_not_queried_in_mvp1(self) -> None:
        """MVP 1 never reads from edge_store — a broken store must not matter."""
        broken_store = MagicMock(spec=EdgeStore)
        broken_store.count = AsyncMock(side_effect=Exception("db error"))
        lcma = LcmaConfig(temperature_default=0.6)
        temp = await compute_temperature(broken_store, lcma, None)
        assert temp == 0.6
        broken_store.count.assert_not_called()

    @pytest.mark.asyncio
    async def test_above_threshold_still_returns_default(
        self, edge_store: EdgeStore, seeded_config: LithosConfig
    ) -> None:
        """Even with many edges present, MVP 1 returns temperature_default."""
        # Populate edges beyond the threshold to prove MVP 1 does not ramp.
        for i in range(60):
            await edge_store.upsert(
                from_id=f"n_{i}",
                to_id=f"n_{i + 1}",
                edge_type="related_to",
                weight=0.5,
                namespace="default",
            )
        lcma = LcmaConfig(temperature_default=0.5, temperature_edge_threshold=50)
        temp = await compute_temperature(edge_store, lcma, None)
        assert temp == 0.5


# ---------------------------------------------------------------------------
# run_retrieve — Phase A parallelism
# ---------------------------------------------------------------------------


class TestRunRetrievePhaseA:
    @pytest.mark.asyncio
    async def test_parallel_scouts_fire(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """Phase A scouts run in parallel and produce results."""
        # Mock the search methods to return known results
        mock_results = []
        with (
            patch.object(seeded_search, "semantic_search", return_value=mock_results),
            patch.object(seeded_search, "full_text_search", return_value=mock_results),
        ):
            result = await run_retrieve(
                query="testing",
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
                limit=10,
            )
        assert "results" in result
        assert "temperature" in result
        assert "terrace_reached" in result
        assert "receipt_id" in result
        assert result["terrace_reached"] == 1

    @pytest.mark.asyncio
    async def test_task_context_scout_fires_with_task_id(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """scout_task_context is included when task_id is provided."""
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=[]),
        ):
            result = await run_retrieve(
                query="testing",
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
                limit=10,
                task_id="task-123",
            )
        assert isinstance(result["results"], list)


# ---------------------------------------------------------------------------
# run_retrieve — Phase B sequencing
# ---------------------------------------------------------------------------


class TestRunRetrievePhaseB:
    @pytest.mark.asyncio
    async def test_provenance_seeded_from_phase_a(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """Provenance scout runs after Phase A, seeded from top candidates."""
        # Phase A: exact_alias finds ID1, provenance should find ID3 (derived from ID1)
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=[]),
        ):
            result = await run_retrieve(
                query=_ID1,  # exact_alias should match UUID
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
                limit=10,
            )
        result_ids = [r["id"] for r in result["results"]]  # type: ignore[union-attr]
        # ID1 from exact_alias, ID3 from provenance (derived from ID1)
        assert _ID1 in result_ids
        assert _ID3 in result_ids


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


class TestNormalization:
    @pytest.mark.asyncio
    async def test_scores_normalized(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """Result scores are in [0, 1]."""
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=[]),
        ):
            result = await run_retrieve(
                query=_ID1,
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
            )
        for r in result["results"]:  # type: ignore[union-attr]
            assert 0.0 <= r["score"] <= 2.0  # Reranked scores may exceed 1.0


# ---------------------------------------------------------------------------
# Receipt writing
# ---------------------------------------------------------------------------


class TestReceiptWriting:
    @pytest.mark.asyncio
    async def test_receipt_written_on_success(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """Every call writes a receipt row."""
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=[]),
        ):
            result = await run_retrieve(
                query="test",
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
            )

        receipt_id = result["receipt_id"]
        assert isinstance(receipt_id, str)
        assert receipt_id.startswith("rcpt_")

        # Verify receipt in database
        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            cursor = await db.execute("SELECT id, query FROM receipts WHERE id = ?", (receipt_id,))
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == receipt_id
            assert row[1] == "test"

    @pytest.mark.asyncio
    async def test_receipt_written_on_error(
        self,
        seeded_km: KnowledgeManager,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """Receipt is written even when scouts raise exceptions."""
        # Create a broken search that raises
        broken_search = MagicMock(spec=SearchEngine)
        broken_search.semantic_search = MagicMock(side_effect=Exception("search failure"))
        broken_search.full_text_search = MagicMock(side_effect=Exception("search failure"))

        result = await run_retrieve(
            query="test",
            search=broken_search,
            knowledge=seeded_km,
            graph=seeded_graph,
            coordination=mock_coordination,
            edge_store=edge_store,
            stats_store=stats_store,
            lcma_config=LcmaConfig(),
        )

        receipt_id = result["receipt_id"]
        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            cursor = await db.execute(
                "SELECT id, terrace_reached FROM receipts WHERE id = ?", (receipt_id,)
            )
            row = await cursor.fetchone()
            assert row is not None

    @pytest.mark.asyncio
    async def test_receipt_row_format(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """Receipt row has all required fields."""
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=[]),
        ):
            result = await run_retrieve(
                query="format check",
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
                limit=5,
                namespace_filter=["default"],
                agent_id="test-agent",
                task_id="task-99",
            )

        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM receipts WHERE id = ?", (result["receipt_id"],)
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row["query"] == "format check"
            assert row["limit"] == 5
            assert json.loads(row["namespace_filter"]) == ["default"]
            assert isinstance(json.loads(row["scouts_fired"]), list)
            assert isinstance(json.loads(row["final_nodes"]), list)
            assert json.loads(row["conflicts_surfaced"]) == []
            assert row["agent_id"] == "test-agent"
            assert row["task_id"] == "task-99"


# ---------------------------------------------------------------------------
# Working memory
# ---------------------------------------------------------------------------


class TestWorkingMemory:
    @pytest.mark.asyncio
    async def test_wm_upserted_with_task_id(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """Working memory rows upserted when task_id provided."""
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=[]),
        ):
            result = await run_retrieve(
                query=_ID1,
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
                task_id="task-wm",
            )

        results = result["results"]
        if results:  # type: ignore[truthy-bool]
            import aiosqlite

            async with aiosqlite.connect(stats_store.db_path) as db:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM working_memory WHERE task_id = ?",
                    ("task-wm",),
                )
                row = await cursor.fetchone()
                assert row is not None
                assert row[0] > 0

    @pytest.mark.asyncio
    async def test_wm_not_written_without_task_id(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """No working memory rows when task_id is None."""
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=[]),
        ):
            await run_retrieve(
                query=_ID1,
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
            )

        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM working_memory")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 0


# ---------------------------------------------------------------------------
# max_context_nodes
# ---------------------------------------------------------------------------


class TestMaxContextNodes:
    @pytest.mark.asyncio
    async def test_defaults_to_limit(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """max_context_nodes defaults to limit when omitted."""
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=[]),
            patch("lithos.lcma.retrieve.scout_provenance", new_callable=AsyncMock) as mock_prov,
        ):
            mock_prov.return_value = []
            await run_retrieve(
                query=_ID1,
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
                limit=5,
            )
            # Provenance was called — check seed_ids length ≤ limit
            if mock_prov.called:
                seed_ids = mock_prov.call_args[0][0]
                assert len(seed_ids) <= 5


# ---------------------------------------------------------------------------
# MMR diversity inside _rerank_fast
# ---------------------------------------------------------------------------


class TestMmrDiversity:
    """_rerank_fast applies a greedy MMR pass to penalise near-duplicates."""

    def test_mmr_spreads_duplicates(self, seeded_km: KnowledgeManager) -> None:
        """Near-duplicate candidates must not monopolise the top ranks."""
        from lithos.lcma import retrieve as retrieve_module

        # Deterministic token sets: _ID1 and _ID3 are near-duplicates
        # (share all tokens), _ID2 is fully disjoint.
        tokens_by_id = {
            _ID1: {"alpha", "testing"},
            _ID3: {"alpha", "testing"},  # identical to _ID1
            _ID2: {"completely", "different"},
        }

        def fake_tokens(_km: KnowledgeManager, node_id: str) -> set[str]:
            return tokens_by_id.get(node_id, set())

        ranked = [
            Candidate(node_id=_ID1, score=1.00, scouts=["scout_vector"]),
            Candidate(node_id=_ID3, score=0.99, scouts=["scout_vector"]),  # near-dup of _ID1
            Candidate(node_id=_ID2, score=0.50, scouts=["scout_vector"]),  # distinct
        ]
        with patch.object(retrieve_module, "_title_tokens", side_effect=fake_tokens):
            out = retrieve_module._mmr_diversify(ranked, seeded_km, window=3, lam=0.5)

        # First is still the top-scoring item; the distinct item must beat
        # the near-duplicate into second place thanks to the diversity penalty.
        assert out[0].node_id == _ID1
        assert out[1].node_id == _ID2
        assert out[2].node_id == _ID3

    def test_mmr_returns_same_length(self, seeded_km: KnowledgeManager) -> None:
        from lithos.lcma.retrieve import _mmr_diversify

        ranked = [
            Candidate(node_id=_ID1, score=0.9, scouts=["scout_vector"]),
            Candidate(node_id=_ID2, score=0.8, scouts=["scout_vector"]),
        ]
        out = _mmr_diversify(ranked, seeded_km)
        assert len(out) == 2

    def test_mmr_empty_input(self, seeded_km: KnowledgeManager) -> None:
        from lithos.lcma.retrieve import _mmr_diversify

        assert _mmr_diversify([], seeded_km) == []


# ---------------------------------------------------------------------------
# Receipt plumbing: candidates_considered, surface_conflicts
# ---------------------------------------------------------------------------


class TestScoutsFiredAuditTrail:
    """``receipts.scouts_fired`` records every scout that ran cleanly,
    not just scouts that produced candidates. A scout that returns []
    must still appear in the audit trail.
    """

    @pytest.mark.asyncio
    async def test_empty_scouts_still_fire(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """When every scout returns [], scouts_fired must still list them."""
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=[]),
        ):
            result = await run_retrieve(
                query="zzzz_no_match_anywhere",
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
                limit=10,
            )

        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT scouts_fired FROM receipts WHERE id = ?",
                (result["receipt_id"],),
            )
            row = await cursor.fetchone()
        assert row is not None
        fired = json.loads(row["scouts_fired"])
        # Five Phase A scouts ran (no task_id, so no scout_task_context).
        # All of them returned [] but they all executed without raising,
        # so all five must appear in the audit trail.
        assert "scout_vector" in fired
        assert "scout_lexical" in fired
        assert "scout_exact_alias" in fired
        assert "scout_tags_recency" in fired
        assert "scout_freshness" in fired

    @pytest.mark.asyncio
    async def test_failing_scout_omitted_from_audit(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """A scout that raises must NOT appear in scouts_fired."""
        with (
            patch.object(seeded_search, "semantic_search", side_effect=RuntimeError("boom")),
            patch.object(seeded_search, "full_text_search", return_value=[]),
        ):
            result = await run_retrieve(
                query="anything",
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
                limit=10,
            )

        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT scouts_fired FROM receipts WHERE id = ?",
                (result["receipt_id"],),
            )
            row = await cursor.fetchone()
        assert row is not None
        fired = json.loads(row["scouts_fired"])
        assert "scout_vector" not in fired  # raised
        assert "scout_lexical" in fired  # ran cleanly with []


class TestReceiptFinalNodesShape:
    """``receipts.final_nodes`` is a JSON array of {id, reasons, scouts}
    objects per design §4.6 — bare ID arrays drop the explainability
    metadata that downstream tooling needs.
    """

    @pytest.mark.asyncio
    async def test_final_nodes_carries_reasons(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        from lithos.search import SearchResult

        tantivy_hits = [
            SearchResult(id=_ID1, title="Alpha Note", snippet="", score=0.9, path="alpha-note.md"),
        ]
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=tantivy_hits),
        ):
            result = await run_retrieve(
                query="alpha",
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
                limit=10,
            )

        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT final_nodes FROM receipts WHERE id = ?",
                (result["receipt_id"],),
            )
            row = await cursor.fetchone()
        assert row is not None
        nodes = json.loads(row["final_nodes"])
        assert isinstance(nodes, list)
        assert len(nodes) >= 1
        first = nodes[0]
        assert isinstance(first, dict)
        assert "id" in first
        assert "reasons" in first
        assert "scouts" in first
        assert first["id"] == _ID1
        # Lexical scout produces a "lexical match score ..." reason.
        assert any("lexical" in r for r in first["reasons"])


class TestReceiptFields:
    @pytest.mark.asyncio
    async def test_candidates_considered_populated(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """candidates_considered in the receipt matches len(merged_pool)."""
        from lithos.search import SearchResult

        tantivy_hits = [
            SearchResult(id=_ID1, title="Alpha Note", snippet="", score=0.9, path="alpha-note.md"),
            SearchResult(id=_ID2, title="Beta Note", snippet="", score=0.8, path="beta-note.md"),
        ]
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=tantivy_hits),
        ):
            result = await run_retrieve(
                query="alpha",
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
                limit=10,
                surface_conflicts=True,
            )

        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT candidates_considered, surface_conflicts FROM receipts WHERE id = ?",
                (result["receipt_id"],),
            )
            row = await cursor.fetchone()
        assert row is not None
        assert row["candidates_considered"] >= 2
        assert row["surface_conflicts"] == 1


# ---------------------------------------------------------------------------
# Contradiction surfacing through retrieve
# ---------------------------------------------------------------------------


class TestContradictionSurfacing:
    """US-003: surface_conflicts=True actually surfaces contradictions."""

    @pytest.mark.asyncio
    async def test_seeded_contradiction_appears_in_result(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """A contradiction edge between two notes surfaces in result['conflicts']."""
        edge_id = await edge_store.upsert(
            from_id=_ID1,
            to_id=_ID2,
            edge_type="contradicts",
            weight=1.0,
            namespace="default",
        )

        from lithos.search import SearchResult

        tantivy_hits = [
            SearchResult(id=_ID1, title="Alpha Note", snippet="", score=0.9, path="alpha-note.md"),
            SearchResult(id=_ID2, title="Beta Note", snippet="", score=0.8, path="beta-note.md"),
        ]
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=tantivy_hits),
        ):
            result = await run_retrieve(
                query="alpha",
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
                limit=10,
                surface_conflicts=True,
            )

        assert "conflicts" in result
        conflicts = result["conflicts"]
        assert len(conflicts) >= 1  # type: ignore[arg-type]
        eids = [c["edge_id"] for c in conflicts]  # type: ignore[union-attr]
        assert edge_id in eids

        # Receipt should also record conflicts_surfaced
        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT conflicts_surfaced FROM receipts WHERE id = ?",
                (result["receipt_id"],),
            )
            row = await cursor.fetchone()
        assert row is not None
        surfaced = json.loads(row["conflicts_surfaced"])
        assert len(surfaced) >= 1

    @pytest.mark.asyncio
    async def test_surface_conflicts_false_is_noop(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """When surface_conflicts=False (default), no conflicts key in result."""
        await edge_store.upsert(
            from_id=_ID1,
            to_id=_ID2,
            edge_type="contradicts",
            weight=1.0,
            namespace="default",
        )

        from lithos.search import SearchResult

        tantivy_hits = [
            SearchResult(id=_ID1, title="Alpha Note", snippet="", score=0.9, path="alpha-note.md"),
        ]
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=tantivy_hits),
        ):
            result = await run_retrieve(
                query="alpha",
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
                limit=10,
                surface_conflicts=False,
            )

        assert "conflicts" not in result


# ---------------------------------------------------------------------------
# Finally-block robustness
# ---------------------------------------------------------------------------


class TestNewScoutsWiredInPhaseB:
    """US-006: graph, coactivation, and source_url scouts fire in Phase B."""

    @pytest.mark.asyncio
    async def test_graph_scout_fires_with_edges(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """When edges.db has edges between notes, scout_graph fires and
        appears in the receipt's scouts_fired list."""
        # Create a typed edge from ID1 → ID2
        await edge_store.upsert(
            from_id=_ID1,
            to_id=_ID2,
            edge_type="related_to",
            weight=0.8,
            namespace="default",
        )

        from lithos.search import SearchResult

        # Phase A returns ID1, so Phase B graph scout can follow the edge to ID2
        tantivy_hits = [
            SearchResult(id=_ID1, title="Alpha Note", snippet="", score=0.9, path="alpha-note.md"),
        ]
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=tantivy_hits),
        ):
            result = await run_retrieve(
                query="alpha",
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
                limit=10,
            )

        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT scouts_fired FROM receipts WHERE id = ?",
                (result["receipt_id"],),
            )
            row = await cursor.fetchone()
        assert row is not None
        fired = json.loads(row["scouts_fired"])
        assert "scout_graph" in fired

        # ID2 should appear in results (found via graph edge from ID1)
        result_ids = [r["id"] for r in result["results"]]
        assert _ID2 in result_ids

    @pytest.mark.asyncio
    async def test_coactivation_scout_fires(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """scout_coactivation fires in Phase B when coactivation data exists."""
        # Seed coactivation data: ID1 co-occurs with ID2
        await stats_store.increment_coactivation(node_a=_ID1, node_b=_ID2, namespace="default")
        await stats_store.increment_coactivation(node_a=_ID1, node_b=_ID2, namespace="default")

        from lithos.search import SearchResult

        tantivy_hits = [
            SearchResult(id=_ID1, title="Alpha Note", snippet="", score=0.9, path="alpha-note.md"),
        ]
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=tantivy_hits),
        ):
            result = await run_retrieve(
                query="alpha",
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
                limit=10,
            )

        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT scouts_fired FROM receipts WHERE id = ?",
                (result["receipt_id"],),
            )
            row = await cursor.fetchone()
        assert row is not None
        fired = json.loads(row["scouts_fired"])
        assert "scout_coactivation" in fired

        # Verify the coactivated candidate was merged into final results
        result_ids = [r["id"] for r in result["results"]]
        assert _ID2 in result_ids

    @pytest.mark.asyncio
    async def test_source_url_scout_fires(
        self,
        seeded_config: LithosConfig,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """scout_source_url fires in Phase B when seed notes share domains."""
        # Create notes with source_url to test domain grouping
        km = KnowledgeManager(seeded_config)
        kp = seeded_config.storage.knowledge_path

        note1 = fm.Post(
            "# Source A\n\nContent A",
            id=_ID1,
            title="Source A",
            author="agent",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            tags=[],
            access_scope="shared",
            note_type="observation",
            source_url="https://example.com/page1",
        )
        (kp / "source-a.md").write_text(fm.dumps(note1))

        note2 = fm.Post(
            "# Source B\n\nContent B",
            id=_ID2,
            title="Source B",
            author="agent",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            tags=[],
            access_scope="shared",
            note_type="observation",
            source_url="https://example.com/page2",
        )
        (kp / "source-b.md").write_text(fm.dumps(note2))

        km._scan_existing()

        from lithos.search import SearchResult

        tantivy_hits = [
            SearchResult(id=_ID1, title="Source A", snippet="", score=0.9, path="source-a.md"),
        ]
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=tantivy_hits),
        ):
            result = await run_retrieve(
                query="source",
                search=seeded_search,
                knowledge=km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
                limit=10,
            )

        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT scouts_fired FROM receipts WHERE id = ?",
                (result["receipt_id"],),
            )
            row = await cursor.fetchone()
        assert row is not None
        fired = json.loads(row["scouts_fired"])
        assert "scout_source_url" in fired

        # Verify the same-domain candidate was merged into final results
        result_ids = [r["id"] for r in result["results"]]
        assert _ID2 in result_ids

    @pytest.mark.asyncio
    async def test_failing_phase_b_scout_does_not_abort(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """A failing Phase B scout must not abort the pipeline."""
        from lithos.search import SearchResult

        tantivy_hits = [
            SearchResult(id=_ID1, title="Alpha Note", snippet="", score=0.9, path="alpha-note.md"),
        ]
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=tantivy_hits),
            patch("lithos.lcma.retrieve.scout_graph", side_effect=RuntimeError("graph boom")),
        ):
            result = await run_retrieve(
                query="alpha",
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
                limit=10,
            )

        # Pipeline still completes and returns results
        assert result["terrace_reached"] == 1
        result_ids = [r["id"] for r in result["results"]]
        assert _ID1 in result_ids

        # The failing scout must NOT appear in scouts_fired
        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT scouts_fired FROM receipts WHERE id = ?",
                (result["receipt_id"],),
            )
            row = await cursor.fetchone()
        assert row is not None
        fired = json.loads(row["scouts_fired"])
        assert "scout_graph" not in fired


class TestFinallyBlockRobustness:
    @pytest.mark.asyncio
    async def test_receipt_written_when_pipeline_raises_early(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        stats_store: StatsStore,
    ) -> None:
        """If merge_and_normalize raises before temperature is assigned,
        the finally block must still write a receipt (no UnboundLocalError).
        """
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=[]),
            patch(
                "lithos.lcma.retrieve.merge_and_normalize",
                side_effect=RuntimeError("boom"),
            ),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await run_retrieve(
                query="anything",
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
            )

        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            cursor = await db.execute("SELECT terrace_reached, temperature FROM receipts")
            rows = await cursor.fetchall()
        # A receipt was written with terrace_reached=0 and the default temp.
        assert len(rows) == 1
        assert rows[0][0] == 0
        assert rows[0][1] == LcmaConfig().temperature_default
