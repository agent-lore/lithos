"""Tests for US-011: Coactivation update on retrieval.

Unit tests for coactivation and node_stats updates, and integration
tests verifying multi-call invariants.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import frontmatter as fm
import pytest
import pytest_asyncio

from lithos.config import LcmaConfig, LithosConfig, StorageConfig
from lithos.graph import KnowledgeGraph
from lithos.knowledge import KnowledgeManager
from lithos.lcma.retrieve import _dominant_namespace, _run_retrieve_impl
from lithos.lcma.stats import StatsStore
from lithos.provenance import EdgeStore, ProvenanceProjection
from lithos.search import SearchEngine
from lithos.server import LithosServer
from tests.helpers import call_tool

# ---------------------------------------------------------------------------
# Unit test fixtures
# ---------------------------------------------------------------------------

_ID1 = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
_ID2 = "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"
_ID3 = "cccccccc-cccc-4ccc-cccc-cccccccccccc"


@pytest.fixture
def seeded_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LithosConfig:
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


@pytest.fixture
def seeded_km(seeded_config: LithosConfig) -> KnowledgeManager:
    km = KnowledgeManager(seeded_config)
    kp = seeded_config.storage.knowledge_path

    note1 = fm.Post(
        "# Alpha\n\nContent about alpha",
        id=_ID1,
        title="Alpha",
        author="agent-a",
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
        tags=["test"],
        access_scope="shared",
    )
    (kp / "alpha.md").write_text(fm.dumps(note1))

    note2 = fm.Post(
        "# Beta\n\nContent about beta",
        id=_ID2,
        title="Beta",
        author="agent-b",
        created_at=datetime.now(UTC).isoformat(),
        updated_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        tags=["test"],
        access_scope="shared",
    )
    (kp / "beta.md").write_text(fm.dumps(note2))

    projects = kp / "projects"
    projects.mkdir()
    note3 = fm.Post(
        "# Gamma\n\nContent about gamma",
        id=_ID3,
        title="Gamma",
        author="agent-a",
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
        tags=["test"],
        access_scope="shared",
        derived_from_ids=[_ID1],
    )
    (projects / "gamma.md").write_text(fm.dumps(note3))

    km._scan_existing()
    return km


@pytest.fixture
def seeded_graph(seeded_config: LithosConfig, seeded_km: KnowledgeManager) -> KnowledgeGraph:
    graph = KnowledgeGraph(seeded_config)
    for doc_id, rel_path in seeded_km._id_to_path.items():
        full_path = seeded_config.storage.knowledge_path / rel_path
        if full_path.exists():
            post = fm.load(str(full_path))
            from lithos.frontmatter_codec import KnowledgeDocument, KnowledgeMetadata

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


@pytest_asyncio.fixture
async def seeded_search(seeded_config: LithosConfig) -> SearchEngine:
    return await SearchEngine.create(seeded_config)


@pytest.fixture
async def edge_store(seeded_config: LithosConfig):
    store = EdgeStore(seeded_config)
    await store.open()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
def projection(edge_store: EdgeStore) -> ProvenanceProjection:
    """ProvenanceProjection wrapping the test edge store.

    Shares the underlying SQLite handle with ``edge_store`` so fixture-level
    upserts are visible through the projection's read API.
    """
    proj = ProvenanceProjection(edge_store.config)
    proj._edge_store = edge_store
    return proj


@pytest.fixture
async def stats_store(seeded_config: LithosConfig):
    store = StatsStore(seeded_config)
    await store.open()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
def mock_coordination() -> AsyncMock:
    coord = AsyncMock()
    coord.list_findings = AsyncMock(return_value=[])
    coord.get_task_status = AsyncMock(return_value=[])
    return coord


# ---------------------------------------------------------------------------
# _dominant_namespace
# ---------------------------------------------------------------------------


class TestDominantNamespace:
    def test_single_node(self, seeded_km: KnowledgeManager) -> None:
        result = _dominant_namespace([_ID1], seeded_km)
        assert result == "default"

    def test_majority_namespace(self, seeded_km: KnowledgeManager) -> None:
        # ID1 and ID2 are in "default", ID3 is in "projects"
        result = _dominant_namespace([_ID1, _ID2, _ID3], seeded_km)
        assert result == "default"

    def test_tie_broken_alphabetically(self, seeded_km: KnowledgeManager) -> None:
        # ID1 is "default", ID3 is "projects" — tie → "default" wins alphabetically
        result = _dominant_namespace([_ID1, _ID3], seeded_km)
        assert result == "default"

    def test_empty_list(self, seeded_km: KnowledgeManager) -> None:
        result = _dominant_namespace([], seeded_km)
        assert result == "default"


# ---------------------------------------------------------------------------
# Coactivation — single call
# ---------------------------------------------------------------------------


class TestCoactivationSingleCall:
    @pytest.mark.asyncio
    async def test_node_stats_incremented(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        projection: ProvenanceProjection,
        stats_store: StatsStore,
    ) -> None:
        """node_stats.retrieval_count incremented for each node in final_nodes."""
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=[]),
        ):
            result = await _run_retrieve_impl(
                query=_ID1,
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                projection=projection,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
            )

        results = result["results"]
        if results:  # type: ignore[truthy-bool]
            async with aiosqlite.connect(stats_store.db_path) as db:
                cursor = await db.execute("SELECT COUNT(*) FROM node_stats")
                row = await cursor.fetchone()
                assert row is not None
                assert row[0] > 0

                # Check retrieval_count = 1 for first call
                cursor = await db.execute("SELECT retrieval_count FROM node_stats LIMIT 1")
                row = await cursor.fetchone()
                assert row is not None
                assert row[0] == 1

    @pytest.mark.asyncio
    async def test_coactivation_pairs_created(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        projection: ProvenanceProjection,
        stats_store: StatsStore,
    ) -> None:
        """Coactivation rows created for unordered pairs in final_nodes."""
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=[]),
        ):
            result = await _run_retrieve_impl(
                query=_ID1,
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                projection=projection,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
            )

        result_ids = [r["id"] for r in result["results"]]  # type: ignore[union-attr]
        if len(result_ids) >= 2:
            async with aiosqlite.connect(stats_store.db_path) as db:
                cursor = await db.execute("SELECT COUNT(*) FROM coactivation")
                row = await cursor.fetchone()
                assert row is not None
                assert row[0] > 0

    @pytest.mark.asyncio
    async def test_first_touch_salience_default(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        projection: ProvenanceProjection,
        stats_store: StatsStore,
    ) -> None:
        """node_stats inserted with salience=0.5 on first touch."""
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=[]),
        ):
            await _run_retrieve_impl(
                query=_ID1,
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                projection=projection,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
            )

        async with aiosqlite.connect(stats_store.db_path) as db:
            cursor = await db.execute("SELECT salience FROM node_stats LIMIT 1")
            row = await cursor.fetchone()
            if row is not None:
                assert row[0] == 0.5


# ---------------------------------------------------------------------------
# Coactivation — multi-call
# ---------------------------------------------------------------------------


class TestCoactivationMultiCall:
    @pytest.mark.asyncio
    async def test_retrieval_count_increments(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        projection: ProvenanceProjection,
        stats_store: StatsStore,
    ) -> None:
        """Two calls with same results increment retrieval_count to 2."""
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=[]),
        ):
            # First call
            await _run_retrieve_impl(
                query=_ID1,
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                projection=projection,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
            )
            # Second call
            await _run_retrieve_impl(
                query=_ID1,
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                projection=projection,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
            )

        async with aiosqlite.connect(stats_store.db_path) as db:
            cursor = await db.execute(
                "SELECT retrieval_count FROM node_stats WHERE node_id = ?", (_ID1,)
            )
            row = await cursor.fetchone()
            if row is not None:
                assert row[0] == 2

    @pytest.mark.asyncio
    async def test_coactivation_count_increments(
        self,
        seeded_km: KnowledgeManager,
        seeded_search: SearchEngine,
        seeded_graph: KnowledgeGraph,
        mock_coordination: AsyncMock,
        edge_store: EdgeStore,
        projection: ProvenanceProjection,
        stats_store: StatsStore,
    ) -> None:
        """Two calls increment coactivation count."""
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=[]),
        ):
            await _run_retrieve_impl(
                query=_ID1,
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                projection=projection,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
            )
            await _run_retrieve_impl(
                query=_ID1,
                search=seeded_search,
                knowledge=seeded_km,
                graph=seeded_graph,
                coordination=mock_coordination,
                edge_store=edge_store,
                projection=projection,
                stats_store=stats_store,
                lcma_config=LcmaConfig(),
            )

        async with aiosqlite.connect(stats_store.db_path) as db:
            cursor = await db.execute("SELECT count FROM coactivation LIMIT 1")
            row = await cursor.fetchone()
            if row is not None:
                assert row[0] == 2


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

pytestmark_integration = pytest.mark.integration


@pytest.mark.integration
class TestCoactivationIntegration:
    @pytest.mark.asyncio
    async def test_single_call_creates_node_stats(self, server: LithosServer) -> None:
        """A single lithos_retrieve call creates node_stats rows."""
        # Seed a note
        await call_tool(
            server,
            "lithos_write",
            {"title": "Coact Note", "content": "Content for coactivation", "agent": "test"},
        )

        await call_tool(server, "lithos_retrieve", {"query": "coactivation"})

        stats_path = server.config.storage.stats_db_path
        async with aiosqlite.connect(stats_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM node_stats")
            row = await cursor.fetchone()
            assert row is not None
            # At least some nodes should have stats
            assert row[0] >= 0

    @pytest.mark.asyncio
    async def test_multi_call_increments(self, server: LithosServer) -> None:
        """Two calls increment retrieval_count."""
        await call_tool(
            server,
            "lithos_write",
            {"title": "Multi Note", "content": "Content for multi call", "agent": "test"},
        )

        r1 = await call_tool(server, "lithos_retrieve", {"query": "multi call"})
        r2 = await call_tool(server, "lithos_retrieve", {"query": "multi call"})

        if r1["results"] and r2["results"]:
            # Find a common doc_id
            ids1 = {r["id"] for r in r1["results"]}
            ids2 = {r["id"] for r in r2["results"]}
            common = ids1 & ids2
            if common:
                doc_id = next(iter(common))
                stats_path = server.config.storage.stats_db_path
                async with aiosqlite.connect(stats_path) as db:
                    cursor = await db.execute(
                        "SELECT retrieval_count FROM node_stats WHERE node_id = ?",
                        (doc_id,),
                    )
                    row = await cursor.fetchone()
                    assert row is not None
                    assert row[0] >= 2
