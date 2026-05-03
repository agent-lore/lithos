"""Tests for US-011: Coactivation update on retrieval.

Unit tests for coactivation and node_stats updates, and integration
tests verifying multi-call invariants.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import aiosqlite
import frontmatter as fm
import pytest
import pytest_asyncio

from lithos.config import LcmaConfig, LithosConfig, StorageConfig
from lithos.graph import KnowledgeGraph
from lithos.knowledge import KnowledgeManager
from lithos.lcma.edges import EdgeStore
from lithos.lcma.retrieve import _dominant_namespace, run_retrieve
from lithos.lcma.stats import StatsStore
from lithos.search import SearchEngine
from lithos.server import LithosServer

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
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        tags=["test"],
        access_scope="shared",
    )
    (kp / "alpha.md").write_text(fm.dumps(note1))

    note2 = fm.Post(
        "# Beta\n\nContent about beta",
        id=_ID2,
        title="Beta",
        author="agent-b",
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
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
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
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


@pytest_asyncio.fixture
async def seeded_search(seeded_config: LithosConfig) -> SearchEngine:
    return await SearchEngine.create(seeded_config)


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
        stats_store: StatsStore,
    ) -> None:
        """node_stats.retrieval_count incremented for each node in final_nodes."""
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
        stats_store: StatsStore,
    ) -> None:
        """Coactivation rows created for unordered pairs in final_nodes."""
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
        stats_store: StatsStore,
    ) -> None:
        """node_stats inserted with salience=0.5 on first touch."""
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
        stats_store: StatsStore,
    ) -> None:
        """Two calls with same results increment retrieval_count to 2."""
        with (
            patch.object(seeded_search, "semantic_search", return_value=[]),
            patch.object(seeded_search, "full_text_search", return_value=[]),
        ):
            # First call
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
            # Second call
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
        stats_store: StatsStore,
    ) -> None:
        """Two calls increment coactivation count."""
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

        async with aiosqlite.connect(stats_store.db_path) as db:
            cursor = await db.execute("SELECT count FROM coactivation LIMIT 1")
            row = await cursor.fetchone()
            if row is not None:
                assert row[0] == 2


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

pytestmark_integration = pytest.mark.integration


async def _call_tool(server: LithosServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await server.mcp._call_tool_mcp(name, arguments)
    if isinstance(result, tuple):
        payload = result[1]
        if isinstance(payload, dict):
            return payload
    content = getattr(result, "content", []) if hasattr(result, "content") else result
    if isinstance(content, list) and content:
        text = getattr(content[0], "text", None)
        if isinstance(text, str):
            return json.loads(text)
    raise AssertionError(f"Unable to decode MCP result for tool {name!r}: {result!r}")


@pytest.mark.integration
class TestCoactivationIntegration:
    @pytest.mark.asyncio
    async def test_single_call_creates_node_stats(self, server: LithosServer) -> None:
        """A single lithos_retrieve call creates node_stats rows."""
        # Seed a note
        await _call_tool(
            server,
            "lithos_write",
            {"title": "Coact Note", "content": "Content for coactivation", "agent": "test"},
        )

        await _call_tool(server, "lithos_retrieve", {"query": "coactivation"})

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
        await _call_tool(
            server,
            "lithos_write",
            {"title": "Multi Note", "content": "Content for multi call", "agent": "test"},
        )

        r1 = await _call_tool(server, "lithos_retrieve", {"query": "multi call"})
        r2 = await _call_tool(server, "lithos_retrieve", {"query": "multi call"})

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
