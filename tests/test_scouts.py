"""Tests for US-008: Scout interface and the seven MVP 1 scouts.

Each scout is tested individually with seeded KnowledgeManager data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import frontmatter as fm
import pytest

from lithos.config import LithosConfig, StorageConfig
from lithos.graph import KnowledgeGraph
from lithos.knowledge import KnowledgeManager
from lithos.lcma.edges import EdgeStore
from lithos.lcma.scouts import (
    ALL_SCOUT_NAMES,
    SCOUT_COACTIVATION,
    SCOUT_EXACT_ALIAS,
    SCOUT_FRESHNESS,
    SCOUT_GRAPH,
    SCOUT_LEXICAL,
    SCOUT_PROVENANCE,
    SCOUT_SOURCE_URL,
    SCOUT_TAGS_RECENCY,
    SCOUT_TASK_CONTEXT,
    SCOUT_VECTOR,
    scout_coactivation,
    scout_contradictions,
    scout_exact_alias,
    scout_freshness,
    scout_graph,
    scout_lexical,
    scout_provenance,
    scout_source_url,
    scout_tags_recency,
    scout_task_context,
    scout_vector,
)
from lithos.lcma.stats import StatsStore
from lithos.search import SearchEngine


@pytest.fixture
def seeded_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LithosConfig:
    """Config with seeded notes for scout testing."""
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
_ID4 = "dddddddd-dddd-4ddd-dddd-dddddddddddd"
_ID5 = "eeeeeeee-eeee-4eee-eeee-eeeeeeeeeeee"
_ID6 = "ffffffff-ffff-4fff-ffff-ffffffffffff"


@pytest.fixture
def seeded_km(seeded_config: LithosConfig) -> KnowledgeManager:
    """KnowledgeManager with seeded notes."""
    km = KnowledgeManager(seeded_config)
    kp = seeded_config.storage.knowledge_path

    # Note 1: shared note in default namespace
    note1 = fm.Post(
        "# Note One\n\nSome content about testing",
        id=_ID1,
        title="Note One",
        author="agent-alpha",
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        tags=["testing", "alpha"],
        aliases=["note-one-alias"],
        access_scope="shared",
        namespace="default",
    )
    (kp / "note-one.md").write_text(fm.dumps(note1))

    # Note 2: shared note in "projects" namespace
    projects_dir = kp / "projects"
    projects_dir.mkdir()
    note2 = fm.Post(
        "# Note Two\n\nContent about projects and updates",
        id=_ID2,
        title="Note Two",
        author="agent-beta",
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        tags=["projects"],
        access_scope="shared",
    )
    (projects_dir / "note-two.md").write_text(fm.dumps(note2))

    # Note 3: task-scoped note
    note3 = fm.Post(
        "# Task Note\n\nThis is a task-scoped note",
        id=_ID3,
        title="Task Note",
        author="agent-alpha",
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        tags=["task"],
        access_scope="task",
        source="task-42",
    )
    (kp / "task-note.md").write_text(fm.dumps(note3))

    # Note 4: agent-private note
    note4 = fm.Post(
        "# Private Note\n\nAgent-private content",
        id=_ID4,
        title="Private Note",
        author="agent-alpha",
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        tags=["private"],
        access_scope="agent_private",
    )
    (kp / "private-note.md").write_text(fm.dumps(note4))

    # Note 5: stale note with expires_at in the past
    note5 = fm.Post(
        "# Stale Note\n\nThis note has expired and needs refresh",
        id=_ID5,
        title="Stale Note",
        author="agent-beta",
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        tags=["stale"],
        expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        access_scope="shared",
    )
    (kp / "stale-note.md").write_text(fm.dumps(note5))

    # Note 6: note with provenance (derives from note 1)
    note6 = fm.Post(
        "# Derived Note\n\nDerived from Note One",
        id=_ID6,
        title="Derived Note",
        author="agent-alpha",
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        derived_from_ids=[_ID1],
        access_scope="shared",
    )
    (kp / "derived-note.md").write_text(fm.dumps(note6))

    # Re-scan to pick up all notes
    km._scan_existing()
    return km


@pytest.fixture
def seeded_graph(seeded_config: LithosConfig, seeded_km: KnowledgeManager) -> KnowledgeGraph:
    """KnowledgeGraph built from seeded notes."""
    graph = KnowledgeGraph(seeded_config)
    # Add nodes for each doc in the KM
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
    """SearchEngine for testing (not indexed, tests mock as needed)."""
    return SearchEngine(seeded_config)


# ---------------------------------------------------------------------------
# scout_vector
# ---------------------------------------------------------------------------


class TestScoutVector:
    @pytest.mark.asyncio
    async def test_returns_candidates_with_correct_scout_name(
        self, seeded_km: KnowledgeManager, seeded_search: SearchEngine
    ) -> None:
        from unittest.mock import patch

        from lithos.search import SemanticResult

        mock_results = [
            SemanticResult(
                id=_ID1,
                title="Note One",
                snippet="content",
                similarity=0.85,
                path="note-one.md",
            ),
        ]
        with patch.object(seeded_search, "semantic_search", return_value=mock_results):
            candidates = await scout_vector("test query", seeded_search, seeded_km, limit=10)
        assert len(candidates) == 1
        assert candidates[0].scouts == [SCOUT_VECTOR]
        assert candidates[0].node_id == _ID1
        assert candidates[0].score == 0.85

    @pytest.mark.asyncio
    async def test_namespace_filter_honoured(
        self, seeded_km: KnowledgeManager, seeded_search: SearchEngine
    ) -> None:
        from unittest.mock import patch

        from lithos.search import SemanticResult

        mock_results = [
            SemanticResult(
                id=_ID1,
                title="Note One",
                snippet="content",
                similarity=0.85,
                path="note-one.md",
            ),
            SemanticResult(
                id=_ID2,
                title="Note Two",
                snippet="content",
                similarity=0.7,
                path="projects/note-two.md",
            ),
        ]
        with patch.object(seeded_search, "semantic_search", return_value=mock_results):
            candidates = await scout_vector(
                "test",
                seeded_search,
                seeded_km,
                namespace_filter=["projects"],
            )
        assert len(candidates) == 1
        assert candidates[0].node_id == _ID2

    @pytest.mark.asyncio
    async def test_access_scope_gating(
        self, seeded_km: KnowledgeManager, seeded_search: SearchEngine
    ) -> None:
        from unittest.mock import patch

        from lithos.search import SemanticResult

        mock_results = [
            SemanticResult(
                id=_ID3,
                title="Task Note",
                snippet="content",
                similarity=0.9,
                path="task-note.md",
            ),
        ]
        with patch.object(seeded_search, "semantic_search", return_value=mock_results):
            # Without matching task_id, task-scoped note excluded
            candidates = await scout_vector("test", seeded_search, seeded_km, task_id=None)
        assert len(candidates) == 0

        with patch.object(seeded_search, "semantic_search", return_value=mock_results):
            # With matching task_id, included
            candidates = await scout_vector("test", seeded_search, seeded_km, task_id="task-42")
        assert len(candidates) == 1


# ---------------------------------------------------------------------------
# scout_lexical
# ---------------------------------------------------------------------------


class TestScoutLexical:
    @pytest.mark.asyncio
    async def test_returns_candidates_with_correct_scout_name(
        self, seeded_km: KnowledgeManager, seeded_search: SearchEngine
    ) -> None:
        from unittest.mock import patch

        from lithos.search import SearchResult

        mock_results = [
            SearchResult(
                id=_ID1,
                title="Note One",
                snippet="content",
                score=5.0,
                path="note-one.md",
            ),
        ]
        with patch.object(seeded_search, "full_text_search", return_value=mock_results):
            candidates = await scout_lexical("test query", seeded_search, seeded_km, limit=10)
        assert len(candidates) == 1
        assert candidates[0].scouts == [SCOUT_LEXICAL]
        assert candidates[0].score == 5.0

    @pytest.mark.asyncio
    async def test_namespace_filter(
        self, seeded_km: KnowledgeManager, seeded_search: SearchEngine
    ) -> None:
        from unittest.mock import patch

        from lithos.search import SearchResult

        mock_results = [
            SearchResult(id=_ID1, title="Note One", snippet="c", score=3.0, path="note-one.md"),
        ]
        with patch.object(seeded_search, "full_text_search", return_value=mock_results):
            candidates = await scout_lexical(
                "test",
                seeded_search,
                seeded_km,
                namespace_filter=["nonexistent"],
            )
        assert len(candidates) == 0


# ---------------------------------------------------------------------------
# scout_exact_alias
# ---------------------------------------------------------------------------


class TestScoutExactAlias:
    @pytest.mark.asyncio
    async def test_resolves_via_graph_link(
        self, seeded_km: KnowledgeManager, seeded_graph: KnowledgeGraph
    ) -> None:
        # Resolve via alias (note-one-alias is an alias for note 1)
        candidates = await scout_exact_alias("note-one-alias", seeded_graph, seeded_km)
        assert len(candidates) >= 1
        assert all(c.scouts == [SCOUT_EXACT_ALIAS] for c in candidates)
        assert all(c.score == 1.0 for c in candidates)
        assert any(c.node_id == _ID1 for c in candidates)

    @pytest.mark.asyncio
    async def test_resolves_via_slug(
        self, seeded_km: KnowledgeManager, seeded_graph: KnowledgeGraph
    ) -> None:
        candidates = await scout_exact_alias("note-one", seeded_graph, seeded_km)
        node_ids = {c.node_id for c in candidates}
        assert _ID1 in node_ids

    @pytest.mark.asyncio
    async def test_uuid_prefix_match(
        self, seeded_km: KnowledgeManager, seeded_graph: KnowledgeGraph
    ) -> None:
        # UUID prefix of _ID1 (aaaaaaaa...)
        candidates = await scout_exact_alias("aaaaaaaa", seeded_graph, seeded_km)
        assert len(candidates) >= 1
        assert any(c.node_id == _ID1 for c in candidates)

    @pytest.mark.asyncio
    async def test_namespace_filter(
        self, seeded_km: KnowledgeManager, seeded_graph: KnowledgeGraph
    ) -> None:
        candidates = await scout_exact_alias(
            _ID1,
            seeded_graph,
            seeded_km,
            namespace_filter=["nonexistent"],
        )
        assert len(candidates) == 0

    @pytest.mark.asyncio
    async def test_access_scope_agent_private(
        self, seeded_km: KnowledgeManager, seeded_graph: KnowledgeGraph
    ) -> None:
        candidates = await scout_exact_alias(_ID4, seeded_graph, seeded_km, agent_id=None)
        assert len(candidates) == 0

        candidates = await scout_exact_alias(_ID4, seeded_graph, seeded_km, agent_id="agent-alpha")
        assert len(candidates) == 1


# ---------------------------------------------------------------------------
# scout_tags_recency
# ---------------------------------------------------------------------------


class TestScoutTagsRecency:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_tags_or_path_prefix(
        self, seeded_km: KnowledgeManager
    ) -> None:
        candidates = await scout_tags_recency("test", seeded_km, tags=None, path_prefix=None)
        assert candidates == []

    @pytest.mark.asyncio
    async def test_returns_candidates_with_tags(self, seeded_km: KnowledgeManager) -> None:
        candidates = await scout_tags_recency("test", seeded_km, tags=["testing"])
        assert len(candidates) >= 1
        assert all(c.scouts == [SCOUT_TAGS_RECENCY] for c in candidates)
        # Scores should be decreasing (recency-ranked)
        for i in range(len(candidates) - 1):
            assert candidates[i].score >= candidates[i + 1].score

    @pytest.mark.asyncio
    async def test_returns_candidates_with_path_prefix(self, seeded_km: KnowledgeManager) -> None:
        candidates = await scout_tags_recency("test", seeded_km, path_prefix="projects")
        assert len(candidates) >= 1

    @pytest.mark.asyncio
    async def test_namespace_filter(self, seeded_km: KnowledgeManager) -> None:
        candidates = await scout_tags_recency(
            "test",
            seeded_km,
            tags=["testing"],
            namespace_filter=["nonexistent"],
        )
        assert len(candidates) == 0


# ---------------------------------------------------------------------------
# scout_freshness
# ---------------------------------------------------------------------------


class TestScoutFreshness:
    @pytest.mark.asyncio
    async def test_returns_empty_without_keyword(self, seeded_km: KnowledgeManager) -> None:
        candidates = await scout_freshness("general query", seeded_km)
        assert candidates == []

    @pytest.mark.asyncio
    async def test_returns_candidates_with_update_keyword(
        self, seeded_km: KnowledgeManager
    ) -> None:
        candidates = await scout_freshness("update the notes", seeded_km)
        assert len(candidates) >= 1
        assert all(c.scouts == [SCOUT_FRESHNESS] for c in candidates)

    @pytest.mark.asyncio
    async def test_stale_notes_get_higher_score(self, seeded_km: KnowledgeManager) -> None:
        candidates = await scout_freshness("please refresh data", seeded_km)
        stale = [c for c in candidates if c.score == 1.0]
        assert len(stale) >= 1  # note-005 is stale

    @pytest.mark.asyncio
    async def test_verify_keyword(self, seeded_km: KnowledgeManager) -> None:
        candidates = await scout_freshness("verify results", seeded_km)
        assert len(candidates) >= 1

    @pytest.mark.asyncio
    async def test_latest_keyword(self, seeded_km: KnowledgeManager) -> None:
        """The 'latest' keyword is part of the freshness signal set (design §5.2)."""
        candidates = await scout_freshness("what's the latest?", seeded_km)
        assert len(candidates) >= 1

    @pytest.mark.asyncio
    async def test_namespace_filter(self, seeded_km: KnowledgeManager) -> None:
        candidates = await scout_freshness(
            "update notes", seeded_km, namespace_filter=["nonexistent"]
        )
        assert len(candidates) == 0


# ---------------------------------------------------------------------------
# scout_provenance
# ---------------------------------------------------------------------------


class TestScoutProvenance:
    @pytest.mark.asyncio
    async def test_forward_walk(self, seeded_km: KnowledgeManager) -> None:
        # note-006 derives from note-001
        candidates = await scout_provenance([_ID6], seeded_km)
        node_ids = {c.node_id for c in candidates}
        assert _ID1 in node_ids
        assert all(c.scouts == [SCOUT_PROVENANCE] for c in candidates)

    @pytest.mark.asyncio
    async def test_reverse_walk(self, seeded_km: KnowledgeManager) -> None:
        # note-001 is a source for note-006
        candidates = await scout_provenance([_ID1], seeded_km)
        node_ids = {c.node_id for c in candidates}
        assert _ID6 in node_ids

    @pytest.mark.asyncio
    async def test_namespace_filter(self, seeded_km: KnowledgeManager) -> None:
        candidates = await scout_provenance([_ID6], seeded_km, namespace_filter=["nonexistent"])
        assert len(candidates) == 0

    @pytest.mark.asyncio
    async def test_access_scope_gating(self, seeded_km: KnowledgeManager) -> None:
        # All provenance-linked notes are shared, so should pass
        candidates = await scout_provenance([_ID6], seeded_km)
        assert len(candidates) >= 1


# ---------------------------------------------------------------------------
# scout_task_context
# ---------------------------------------------------------------------------


class TestScoutTaskContext:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_task_id(self, seeded_km: KnowledgeManager) -> None:
        from unittest.mock import AsyncMock

        coordination = AsyncMock()
        candidates = await scout_task_context(coordination, seeded_km, task_id=None)
        assert candidates == []

    @pytest.mark.asyncio
    async def test_returns_candidates_from_findings(self, seeded_km: KnowledgeManager) -> None:
        from dataclasses import dataclass as dc
        from unittest.mock import AsyncMock

        @dc
        class FakeFinding:
            id: str
            task_id: str
            agent: str
            summary: str
            knowledge_id: str | None
            created_at: datetime

        coordination = AsyncMock()
        coordination.list_findings.return_value = [
            FakeFinding(
                id="f1",
                task_id="task-42",
                agent="agent-alpha",
                summary="Found something",
                knowledge_id=_ID1,
                created_at=datetime.now(timezone.utc),
            ),
            FakeFinding(
                id="f2",
                task_id="task-42",
                agent="agent-alpha",
                summary="Nothing linked",
                knowledge_id=None,  # Should be ignored
                created_at=datetime.now(timezone.utc),
            ),
        ]
        coordination.get_task_status.return_value = []

        candidates = await scout_task_context(coordination, seeded_km, task_id="task-42")
        node_ids = {c.node_id for c in candidates}
        assert _ID1 in node_ids
        assert all(c.scouts == [SCOUT_TASK_CONTEXT] for c in candidates)

    @pytest.mark.asyncio
    async def test_returns_candidates_from_source_field(self, seeded_km: KnowledgeManager) -> None:
        """Notes whose frontmatter `source` matches task_id are surfaced.

        Replaces the previous broader behaviour that pulled in every note
        authored by any agent with a non-expired claim on the task — that
        approach flooded results with unrelated notes from long-lived agents.
        """
        from unittest.mock import AsyncMock

        coordination = AsyncMock()
        coordination.list_findings.return_value = []

        # Note 3 in the fixture has source="task-42" with access_scope="task"
        candidates = await scout_task_context(coordination, seeded_km, task_id="task-42")
        found_ids = {c.node_id for c in candidates}
        assert _ID3 in found_ids
        # Notes without matching source must NOT be surfaced even when
        # they share the same author (agent-alpha also authored _ID1/_ID4/_ID6).
        assert _ID1 not in found_ids
        assert _ID4 not in found_ids  # agent_private, different task
        assert _ID6 not in found_ids  # no source set

    @pytest.mark.asyncio
    async def test_ignores_notes_from_other_tasks(self, seeded_km: KnowledgeManager) -> None:
        """Task ID mismatch means no task-context hits even if source is set."""
        from unittest.mock import AsyncMock

        coordination = AsyncMock()
        coordination.list_findings.return_value = []

        candidates = await scout_task_context(coordination, seeded_km, task_id="task-99")
        assert candidates == []

    @pytest.mark.asyncio
    async def test_namespace_filter(self, seeded_km: KnowledgeManager) -> None:
        from dataclasses import dataclass as dc
        from unittest.mock import AsyncMock

        @dc
        class FakeFinding:
            id: str
            task_id: str
            agent: str
            summary: str
            knowledge_id: str | None
            created_at: datetime

        coordination = AsyncMock()
        coordination.list_findings.return_value = [
            FakeFinding(
                id="f1",
                task_id="task-42",
                agent="agent-alpha",
                summary="Found",
                knowledge_id=_ID1,
                created_at=datetime.now(timezone.utc),
            ),
        ]
        coordination.get_task_status.return_value = []

        candidates = await scout_task_context(
            coordination,
            seeded_km,
            task_id="task-42",
            namespace_filter=["nonexistent"],
        )
        assert len(candidates) == 0


# ---------------------------------------------------------------------------
# scout_graph
# ---------------------------------------------------------------------------

_ID7 = "77777777-7777-4777-7777-777777777777"


@pytest.fixture
def graph_with_links(seeded_config: LithosConfig, seeded_km: KnowledgeManager) -> KnowledgeGraph:
    """KnowledgeGraph where note-1 wiki-links to note-2."""
    from lithos.knowledge import KnowledgeDocument, KnowledgeMetadata, parse_wiki_links

    graph = KnowledgeGraph(seeded_config)
    kp = seeded_config.storage.knowledge_path

    # Rewrite note-1 to include a wiki-link to note-2
    note1 = fm.Post(
        f"# Note One\n\nSome content linking to [[{_ID2}]]",
        id=_ID1,
        title="Note One",
        author="agent-alpha",
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        tags=["testing", "alpha"],
        aliases=["note-one-alias"],
        access_scope="shared",
        namespace="default",
    )
    (kp / "note-one.md").write_text(fm.dumps(note1))
    seeded_km._scan_existing()

    for doc_id, rel_path in seeded_km._id_to_path.items():
        full_path = kp / rel_path
        if full_path.exists():
            post = fm.load(str(full_path))
            metadata = KnowledgeMetadata.from_dict(dict(post.metadata))
            doc = KnowledgeDocument(
                id=doc_id,
                title=metadata.title,
                content=post.content,
                metadata=metadata,
                path=rel_path,
                links=parse_wiki_links(post.content),
            )
            graph.add_document(doc)
    return graph


@pytest.fixture
async def seeded_edge_store(seeded_config: LithosConfig) -> EdgeStore:
    """EdgeStore with some typed edges seeded."""
    store = EdgeStore(seeded_config)
    await store.open()
    # _ID1 -> _ID5 via "related_to" edge with weight 0.8
    await store.upsert(
        from_id=_ID1,
        to_id=_ID5,
        edge_type="related_to",
        weight=0.8,
        namespace="default",
    )
    # _ID2 -> _ID6 via "supports" edge with weight 0.6
    await store.upsert(
        from_id=_ID2,
        to_id=_ID6,
        edge_type="supports",
        weight=0.6,
        namespace="default",
    )
    return store


class TestScoutGraph:
    async def test_wiki_link_neighbors(
        self,
        graph_with_links: KnowledgeGraph,
        seeded_edge_store: EdgeStore,
        seeded_km: KnowledgeManager,
    ) -> None:
        """Wiki-link from note-1 to note-2 should surface note-2."""
        candidates = await scout_graph([_ID1], graph_with_links, seeded_edge_store, seeded_km)
        node_ids = {c.node_id for c in candidates}
        assert _ID2 in node_ids
        assert all(c.scouts == [SCOUT_GRAPH] for c in candidates)

    async def test_typed_edge_neighbors(
        self,
        seeded_graph: KnowledgeGraph,
        seeded_edge_store: EdgeStore,
        seeded_km: KnowledgeManager,
    ) -> None:
        """Typed edge from note-1 to note-5 should surface note-5."""
        candidates = await scout_graph([_ID1], seeded_graph, seeded_edge_store, seeded_km)
        node_ids = {c.node_id for c in candidates}
        assert _ID5 in node_ids

    async def test_dedup_wiki_and_edge(
        self,
        graph_with_links: KnowledgeGraph,
        seeded_edge_store: EdgeStore,
        seeded_km: KnowledgeManager,
    ) -> None:
        """Same neighbor via wiki-link and typed edge should appear once."""
        # Add edge _ID1 -> _ID2 (already wiki-linked)
        await seeded_edge_store.upsert(
            from_id=_ID1,
            to_id=_ID2,
            edge_type="related_to",
            weight=0.9,
            namespace="default",
        )
        candidates = await scout_graph([_ID1], graph_with_links, seeded_edge_store, seeded_km)
        id_counts = [c.node_id for c in candidates].count(_ID2)
        assert id_counts == 1
        # Edge weight (0.9) should win over wiki-link (0.5)
        c2 = next(c for c in candidates if c.node_id == _ID2)
        assert c2.score == 0.9

    async def test_namespace_filter(
        self,
        seeded_graph: KnowledgeGraph,
        seeded_edge_store: EdgeStore,
        seeded_km: KnowledgeManager,
    ) -> None:
        candidates = await scout_graph(
            [_ID1],
            seeded_graph,
            seeded_edge_store,
            seeded_km,
            namespace_filter=["nonexistent"],
        )
        assert len(candidates) == 0

    async def test_edge_score_uses_weight(
        self,
        seeded_graph: KnowledgeGraph,
        seeded_edge_store: EdgeStore,
        seeded_km: KnowledgeManager,
    ) -> None:
        """Typed edge neighbor should use the edge weight as score."""
        candidates = await scout_graph([_ID1], seeded_graph, seeded_edge_store, seeded_km)
        c5 = next((c for c in candidates if c.node_id == _ID5), None)
        assert c5 is not None
        assert c5.score == 0.8

    async def test_excludes_seeds(
        self,
        graph_with_links: KnowledgeGraph,
        seeded_edge_store: EdgeStore,
        seeded_km: KnowledgeManager,
    ) -> None:
        """Seed nodes should not appear in results."""
        candidates = await scout_graph([_ID1, _ID2], graph_with_links, seeded_edge_store, seeded_km)
        node_ids = {c.node_id for c in candidates}
        assert _ID1 not in node_ids
        assert _ID2 not in node_ids

    async def test_tags_filter(
        self,
        seeded_graph: KnowledgeGraph,
        seeded_edge_store: EdgeStore,
        seeded_km: KnowledgeManager,
    ) -> None:
        candidates = await scout_graph(
            [_ID1],
            seeded_graph,
            seeded_edge_store,
            seeded_km,
            tags=["nonexistent-tag"],
        )
        assert len(candidates) == 0


# ---------------------------------------------------------------------------
# scout_coactivation
# ---------------------------------------------------------------------------


@pytest.fixture
async def coactivation_stats_store(
    seeded_config: LithosConfig, seeded_km: KnowledgeManager
) -> StatsStore:
    """StatsStore with seeded coactivation data."""
    store = StatsStore(seeded_config)
    await store.open()
    # _ID1 co-occurs with _ID2 (3x) and _ID5 (1x)
    for _ in range(3):
        await store.increment_coactivation(node_a=_ID1, node_b=_ID2, namespace="default")
    await store.increment_coactivation(node_a=_ID1, node_b=_ID5, namespace="default")
    return store


class TestScoutCoactivation:
    async def test_returns_coactivated_neighbors(
        self, coactivation_stats_store: StatsStore, seeded_km: KnowledgeManager
    ) -> None:
        candidates = await scout_coactivation([_ID1], coactivation_stats_store, seeded_km)
        node_ids = {c.node_id for c in candidates}
        assert _ID2 in node_ids
        assert _ID5 in node_ids
        assert all(c.scouts == [SCOUT_COACTIVATION] for c in candidates)

    async def test_scores_by_count(
        self, coactivation_stats_store: StatsStore, seeded_km: KnowledgeManager
    ) -> None:
        candidates = await scout_coactivation([_ID1], coactivation_stats_store, seeded_km)
        c2 = next(c for c in candidates if c.node_id == _ID2)
        c5 = next(c for c in candidates if c.node_id == _ID5)
        assert c2.score > c5.score  # 3 > 1

    async def test_namespace_filter(
        self, coactivation_stats_store: StatsStore, seeded_km: KnowledgeManager
    ) -> None:
        candidates = await scout_coactivation(
            [_ID1], coactivation_stats_store, seeded_km, namespace_filter=["nonexistent"]
        )
        assert len(candidates) == 0

    async def test_namespace_filter_scopes_coactivation_rows(
        self, seeded_config: LithosConfig, seeded_km: KnowledgeManager
    ) -> None:
        """Coactivation rows stored under a different namespace must be excluded
        even when the candidate note itself lives in the allowed namespace."""
        store = StatsStore(seeded_config)
        await store.open()
        # Record coactivation under namespace "other" — the candidate note (_ID2)
        # lives in the "default" namespace, but the evidence is from "other".
        for _ in range(5):
            await store.increment_coactivation(node_a=_ID1, node_b=_ID2, namespace="other")
        candidates = await scout_coactivation(
            [_ID1], store, seeded_km, namespace_filter=["default"]
        )
        # _ID2 coactivation data is under "other", so it should NOT surface
        node_ids = {c.node_id for c in candidates}
        assert _ID2 not in node_ids

    async def test_empty_seeds(
        self, coactivation_stats_store: StatsStore, seeded_km: KnowledgeManager
    ) -> None:
        candidates = await scout_coactivation([], coactivation_stats_store, seeded_km)
        assert candidates == []


# ---------------------------------------------------------------------------
# scout_source_url
# ---------------------------------------------------------------------------

_URL_ID_A = "11111111-1111-4111-1111-111111111111"
_URL_ID_B = "22222222-2222-4222-2222-222222222222"
_URL_ID_C = "33333333-3333-4333-3333-333333333333"


@pytest.fixture
def source_url_km(seeded_config: LithosConfig) -> KnowledgeManager:
    """KnowledgeManager with notes that have source_url fields."""
    km = KnowledgeManager(seeded_config)
    kp = seeded_config.storage.knowledge_path

    now = datetime.now(timezone.utc).isoformat()

    note_a = fm.Post(
        "# Research A\n\nFrom example.com",
        id=_URL_ID_A,
        title="Research A",
        author="agent-alpha",
        created_at=now,
        updated_at=now,
        access_scope="shared",
        source_url="https://example.com/page1",
    )
    (kp / "research-a.md").write_text(fm.dumps(note_a))

    note_b = fm.Post(
        "# Research B\n\nAlso from example.com",
        id=_URL_ID_B,
        title="Research B",
        author="agent-alpha",
        created_at=now,
        updated_at=now,
        access_scope="shared",
        source_url="https://example.com/page2",
    )
    (kp / "research-b.md").write_text(fm.dumps(note_b))

    note_c = fm.Post(
        "# Research C\n\nFrom different.org",
        id=_URL_ID_C,
        title="Research C",
        author="agent-alpha",
        created_at=now,
        updated_at=now,
        access_scope="shared",
        source_url="https://different.org/article",
    )
    (kp / "research-c.md").write_text(fm.dumps(note_c))

    km._scan_existing()
    return km


class TestScoutSourceUrl:
    async def test_finds_same_domain_notes(self, source_url_km: KnowledgeManager) -> None:
        """Seed A (example.com) should find B (also example.com) but not C (different.org)."""
        candidates = await scout_source_url([_URL_ID_A], source_url_km)
        node_ids = {c.node_id for c in candidates}
        assert _URL_ID_B in node_ids
        assert _URL_ID_C not in node_ids
        assert all(c.scouts == [SCOUT_SOURCE_URL] for c in candidates)

    async def test_no_source_url_returns_empty(self, seeded_km: KnowledgeManager) -> None:
        """Seeds without source_url should return []."""
        candidates = await scout_source_url([_ID1], seeded_km)
        assert candidates == []

    async def test_excludes_seeds(self, source_url_km: KnowledgeManager) -> None:
        candidates = await scout_source_url([_URL_ID_A, _URL_ID_B], source_url_km)
        node_ids = {c.node_id for c in candidates}
        assert _URL_ID_A not in node_ids
        assert _URL_ID_B not in node_ids

    async def test_namespace_filter(self, source_url_km: KnowledgeManager) -> None:
        candidates = await scout_source_url(
            [_URL_ID_A], source_url_km, namespace_filter=["nonexistent"]
        )
        assert len(candidates) == 0

    async def test_seed_not_owner_in_source_url_index(self, seeded_config: LithosConfig) -> None:
        """A seed note with source_url that lost the _source_url_to_id slot
        (due to a collision) must still activate the scout."""
        _COLLISION_A = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
        _COLLISION_B = "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"
        _COLLISION_C = "cccccccc-cccc-4ccc-cccc-cccccccccccc"

        km = KnowledgeManager(seeded_config)
        kp = seeded_config.storage.knowledge_path
        now = datetime.now(timezone.utc).isoformat()

        # Note A owns the source_url slot for example.com/same
        note_a = fm.Post(
            "# A\n\nContent A",
            id=_COLLISION_A,
            title="A",
            author="agent",
            created_at=now,
            updated_at=now,
            access_scope="shared",
            source_url="https://example.com/same",
        )
        (kp / "coll-a.md").write_text(fm.dumps(note_a))

        # Note B has the same normalized source_url → collision, NOT in index
        note_b = fm.Post(
            "# B\n\nContent B",
            id=_COLLISION_B,
            title="B",
            author="agent",
            created_at=now,
            updated_at=now,
            access_scope="shared",
            source_url="https://example.com/same",
        )
        (kp / "coll-b.md").write_text(fm.dumps(note_b))

        # Note C has a different page on the same domain
        note_c = fm.Post(
            "# C\n\nContent C",
            id=_COLLISION_C,
            title="C",
            author="agent",
            created_at=now,
            updated_at=now,
            access_scope="shared",
            source_url="https://example.com/other-page",
        )
        (kp / "coll-c.md").write_text(fm.dumps(note_c))

        km._scan_existing()

        # Confirm B is NOT the owner in _source_url_to_id (A is)
        from lithos.knowledge import normalize_url

        norm = normalize_url("https://example.com/same")
        assert km._source_url_to_id.get(norm) == _COLLISION_A

        # Use B as seed — it has source_url but is NOT in _source_url_to_id
        candidates = await scout_source_url([_COLLISION_B], km)
        node_ids = {c.node_id for c in candidates}
        # Should still find C (same domain) via B's on-disk source_url
        assert _COLLISION_C in node_ids


# ---------------------------------------------------------------------------
# scout_contradictions
# ---------------------------------------------------------------------------


class TestScoutContradictions:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_edges(
        self, seeded_km: KnowledgeManager, seeded_config: LithosConfig
    ) -> None:
        """No contradiction edges → empty list."""
        edge_store = EdgeStore(seeded_config)
        await edge_store.open()
        result = await scout_contradictions([_ID1], edge_store, seeded_km)
        assert result == []

    @pytest.mark.asyncio
    async def test_surfaces_contradiction_edge(
        self, seeded_km: KnowledgeManager, seeded_config: LithosConfig
    ) -> None:
        """Seeded contradiction edge is surfaced."""
        edge_store = EdgeStore(seeded_config)
        await edge_store.open()
        eid = await edge_store.upsert(
            from_id=_ID1,
            to_id=_ID2,
            edge_type="contradicts",
            weight=1.0,
            namespace="default",
        )
        result = await scout_contradictions([_ID1], edge_store, seeded_km)
        assert len(result) == 1
        assert result[0]["edge_id"] == eid
        assert result[0]["from_id"] == _ID1
        assert result[0]["to_id"] == _ID2
        assert result[0]["conflict_state"] is None

    @pytest.mark.asyncio
    async def test_excludes_resolved_edges(
        self, seeded_km: KnowledgeManager, seeded_config: LithosConfig
    ) -> None:
        """Edges with resolved conflict_state are excluded."""
        edge_store = EdgeStore(seeded_config)
        await edge_store.open()
        for state in ("superseded", "refuted", "merged"):
            await edge_store.upsert(
                from_id=_ID1,
                to_id=_ID2,
                edge_type="contradicts",
                weight=1.0,
                namespace=f"ns-{state}",
                conflict_state=state,
            )
        result = await scout_contradictions([_ID1], edge_store, seeded_km)
        assert result == []

    @pytest.mark.asyncio
    async def test_excludes_different_namespace(
        self, seeded_km: KnowledgeManager, seeded_config: LithosConfig
    ) -> None:
        """Contradiction edge to a note in a different namespace is excluded when filter is set."""
        edge_store = EdgeStore(seeded_config)
        await edge_store.open()
        # _ID1 is in "default" namespace, _ID2 is in "projects" namespace
        await edge_store.upsert(
            from_id=_ID1,
            to_id=_ID2,
            edge_type="contradicts",
            weight=1.0,
            namespace="default",
        )
        # Filter to only "default" — _ID2 is in "projects" so should be excluded
        result = await scout_contradictions(
            [_ID1], edge_store, seeded_km, namespace_filter=["default"]
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_excludes_quarantined_note(
        self, seeded_km: KnowledgeManager, seeded_config: LithosConfig
    ) -> None:
        """Contradiction edge pointing to a quarantined note is excluded."""
        edge_store = EdgeStore(seeded_config)
        await edge_store.open()

        # Create a quarantined note
        quarantined_id = "qqqqqqqq-qqqq-4qqq-qqqq-qqqqqqqqqqqq"
        note = fm.Post(
            "# Quarantined\n\nBad content",
            id=quarantined_id,
            title="Quarantined Note",
            author="agent-alpha",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            access_scope="shared",
            namespace="default",
            status="quarantined",
        )
        kp = seeded_config.storage.knowledge_path
        (kp / "quarantined-note.md").write_text(fm.dumps(note))
        seeded_km._scan_existing()

        await edge_store.upsert(
            from_id=_ID1,
            to_id=quarantined_id,
            edge_type="contradicts",
            weight=1.0,
            namespace="default",
        )
        result = await scout_contradictions([_ID1], edge_store, seeded_km)
        assert result == []


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestScoutConstants:
    def test_all_scout_names_has_ten_entries(self) -> None:
        assert len(ALL_SCOUT_NAMES) == 10

    def test_canonical_names(self) -> None:
        assert SCOUT_VECTOR == "scout_vector"
        assert SCOUT_LEXICAL == "scout_lexical"
        assert SCOUT_EXACT_ALIAS == "scout_exact_alias"
        assert SCOUT_TAGS_RECENCY == "scout_tags_recency"
        assert SCOUT_FRESHNESS == "scout_freshness"
        assert SCOUT_PROVENANCE == "scout_provenance"
        assert SCOUT_TASK_CONTEXT == "scout_task_context"
        assert SCOUT_GRAPH == "scout_graph"
        assert SCOUT_COACTIVATION == "scout_coactivation"
        assert SCOUT_SOURCE_URL == "scout_source_url"


# ---------------------------------------------------------------------------
# Global tags / path_prefix filtering across all scouts
# ---------------------------------------------------------------------------


class TestGlobalTagsAndPathFilters:
    """All scouts must honor caller-supplied ``tags`` and ``path_prefix``
    filters even when their underlying backend doesn't pre-filter.
    """

    @pytest.mark.asyncio
    async def test_exact_alias_filters_on_tags(
        self, seeded_km: KnowledgeManager, seeded_graph: KnowledgeGraph
    ) -> None:
        """exact_alias post-filters resolved candidates against tags."""
        # _ID1 is "Note One" with tags=["testing", "alpha"], slug "note-one"
        hits = await scout_exact_alias(
            "note-one",
            seeded_graph,
            seeded_km,
            tags=["testing"],
        )
        assert _ID1 in {c.node_id for c in hits}

        # Filter requiring an unrelated tag should drop it.
        hits = await scout_exact_alias(
            "note-one",
            seeded_graph,
            seeded_km,
            tags=["nonexistent"],
        )
        assert hits == []

    @pytest.mark.asyncio
    async def test_exact_alias_filters_on_path_prefix(
        self, seeded_km: KnowledgeManager, seeded_graph: KnowledgeGraph
    ) -> None:
        """exact_alias drops notes whose path doesn't match the prefix."""
        # _ID2 is "Note Two" under projects/note-two.md, slug "note-two"
        hits = await scout_exact_alias(
            "note-two",
            seeded_graph,
            seeded_km,
            path_prefix="projects",
        )
        assert _ID2 in {c.node_id for c in hits}

        hits = await scout_exact_alias(
            "note-two",
            seeded_graph,
            seeded_km,
            path_prefix="other",
        )
        assert hits == []

    @pytest.mark.asyncio
    async def test_provenance_filters_on_tags(self, seeded_km: KnowledgeManager) -> None:
        """Provenance scout post-filters by tag.

        _ID6 derives from _ID1; _ID1 has tags=["testing","alpha"], _ID6
        has no tags. Filter requiring "testing" should drop _ID6 even if
        the lineage walk returns it.
        """
        hits = await scout_provenance([_ID1], seeded_km, tags=["testing"])
        # _ID6 derives from _ID1 but lacks the testing tag.
        assert _ID6 not in {c.node_id for c in hits}

    @pytest.mark.asyncio
    async def test_provenance_filters_on_path_prefix(self, seeded_km: KnowledgeManager) -> None:
        """Provenance scout drops candidates that fail the path prefix."""
        # _ID6 lives at knowledge root (derived-note.md), so a path_prefix
        # of "projects" must drop it.
        hits = await scout_provenance([_ID1], seeded_km, path_prefix="projects")
        assert _ID6 not in {c.node_id for c in hits}

    @pytest.mark.asyncio
    async def test_freshness_filters_on_tags(self, seeded_km: KnowledgeManager) -> None:
        """Freshness scout passes tags down to list_all so the candidate
        set is pre-filtered (and post-filtered as a defensive net).
        """
        # _ID5 is the stale note with tags=["stale"].
        hits = await scout_freshness("please refresh data", seeded_km, tags=["stale"])
        assert _ID5 in {c.node_id for c in hits}

        hits = await scout_freshness("please refresh data", seeded_km, tags=["nonexistent"])
        assert hits == []

    @pytest.mark.asyncio
    async def test_task_context_filters_on_tags(self, seeded_km: KnowledgeManager) -> None:
        """task_context scout drops notes that fail the tag filter."""
        from unittest.mock import AsyncMock

        coordination = AsyncMock()
        coordination.list_findings.return_value = []

        # _ID3 has source="task-42" and tags=["task"]. Filtering on "task"
        # should still surface it; filtering on "research" should drop it.
        hits = await scout_task_context(coordination, seeded_km, task_id="task-42", tags=["task"])
        assert _ID3 in {c.node_id for c in hits}

        hits = await scout_task_context(
            coordination, seeded_km, task_id="task-42", tags=["research"]
        )
        assert hits == []


# ---------------------------------------------------------------------------
# Explicit namespace override regression test
# ---------------------------------------------------------------------------


class TestExplicitNamespaceOverride:
    """Notes with explicit ``namespace`` frontmatter must use that value
    everywhere — not the path-derived default.

    Regression test for the bug where the metadata cache stored only
    ``path`` and consumers re-derived the namespace from the path,
    silently dropping explicit overrides.
    """

    @pytest.fixture
    def override_km(self, seeded_config: LithosConfig) -> KnowledgeManager:
        kp = seeded_config.storage.knowledge_path
        # Note in projects/ subdir whose path-derived namespace would be
        # "projects" but whose frontmatter says "research/alpha".
        override_dir = kp / "projects"
        override_dir.mkdir(exist_ok=True)
        post = fm.Post(
            "# Override Note\n\nOverride content",
            id="00000000-1111-4111-1111-111111111111",
            title="Override Note",
            author="agent-alpha",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            tags=["override"],
            access_scope="shared",
            namespace="research/alpha",  # explicit override
        )
        (override_dir / "override-note.md").write_text(fm.dumps(post))
        km = KnowledgeManager(seeded_config)
        return km

    def test_cached_meta_keeps_explicit_namespace(self, override_km: KnowledgeManager) -> None:
        cached = override_km._meta_cache["00000000-1111-4111-1111-111111111111"]
        # Path-derived would be "projects"; explicit override is "research/alpha".
        assert cached.namespace == "research/alpha"

    @pytest.mark.asyncio
    async def test_namespace_filter_honors_explicit_override(
        self,
        override_km: KnowledgeManager,
        seeded_search: SearchEngine,
    ) -> None:
        """A namespace_filter for the explicit value matches; the path-derived
        value does NOT match (proves the override is being read everywhere).
        """
        from unittest.mock import patch

        from lithos.search import SemanticResult

        doc_id = "00000000-1111-4111-1111-111111111111"
        mock_results = [
            SemanticResult(
                id=doc_id,
                title="Override Note",
                snippet="content",
                similarity=0.9,
                path="projects/override-note.md",
            ),
        ]

        # 1. Filter on explicit namespace → match
        with patch.object(seeded_search, "semantic_search", return_value=mock_results):
            hits = await scout_vector(
                "anything",
                seeded_search,
                override_km,
                namespace_filter=["research/alpha"],
            )
        assert {c.node_id for c in hits} == {doc_id}

        # 2. Filter on path-derived namespace → no match
        with patch.object(seeded_search, "semantic_search", return_value=mock_results):
            hits = await scout_vector(
                "anything",
                seeded_search,
                override_km,
                namespace_filter=["projects"],
            )
        assert hits == []
