"""Tests for US-012: Internal provenance-to-edges projection.

Unit tests cover: forward projection, stale edge removal, idempotent
repeat runs, and no-op when edges.db absent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import frontmatter as fm
import pytest

from lithos.config import LithosConfig, StorageConfig
from lithos.knowledge import KnowledgeManager
from lithos.lcma.edges import EdgeStore, _project_provenance_to_edges

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ID1 = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
_ID2 = "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"
_ID3 = "cccccccc-cccc-4ccc-cccc-cccccccccccc"
_ID4 = "dddddddd-dddd-4ddd-dddd-dddddddddddd"


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


def _write_note(
    kp: Path,
    *,
    doc_id: str,
    title: str,
    content: str,
    subdir: str | None = None,
    derived_from_ids: list[str] | None = None,
) -> None:
    """Write a note file to disk."""
    now = datetime.now(timezone.utc).isoformat()
    kwargs: dict[str, object] = {
        "id": doc_id,
        "title": title,
        "author": "test",
        "created_at": now,
        "updated_at": now,
        "tags": ["test"],
        "access_scope": "shared",
    }
    if derived_from_ids:
        kwargs["derived_from_ids"] = derived_from_ids

    post = fm.Post(content, **kwargs)
    if subdir:
        target = kp / subdir
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{title.lower().replace(' ', '-')}.md").write_text(fm.dumps(post))
    else:
        (kp / f"{title.lower().replace(' ', '-')}.md").write_text(fm.dumps(post))


@pytest.fixture
def seeded_km(seeded_config: LithosConfig) -> KnowledgeManager:
    """KnowledgeManager with notes: Alpha, Beta (no provenance), Gamma (derived from Alpha)."""
    kp = seeded_config.storage.knowledge_path
    _write_note(kp, doc_id=_ID1, title="Alpha", content="# Alpha\n\nAlpha content")
    _write_note(kp, doc_id=_ID2, title="Beta", content="# Beta\n\nBeta content")
    _write_note(
        kp,
        doc_id=_ID3,
        title="Gamma",
        content="# Gamma\n\nGamma content",
        subdir="projects",
        derived_from_ids=[_ID1],
    )
    km = KnowledgeManager(seeded_config)
    return km


@pytest.fixture
async def edge_store(seeded_config: LithosConfig) -> EdgeStore:
    store = EdgeStore(seeded_config)
    await store.open()
    return store


# ---------------------------------------------------------------------------
# Test: forward projection creates derived_from edges
# ---------------------------------------------------------------------------


class TestForwardProjection:
    @pytest.mark.asyncio
    async def test_creates_derived_from_edges(
        self, seeded_km: KnowledgeManager, edge_store: EdgeStore
    ) -> None:
        """Projection creates derived_from edge for Gamma -> Alpha."""
        result = await _project_provenance_to_edges(edge_store, seeded_km)

        assert result["created"] == 1
        assert result["removed"] == 0

        edges = await edge_store.list_edges(edge_type="derived_from")
        assert len(edges) == 1
        assert edges[0]["from_id"] == _ID3
        assert edges[0]["to_id"] == _ID1
        assert edges[0]["type"] == "derived_from"
        assert edges[0]["namespace"] == "projects"
        assert edges[0]["provenance_type"] == "frontmatter"

    @pytest.mark.asyncio
    async def test_multiple_sources(
        self, seeded_config: LithosConfig, edge_store: EdgeStore
    ) -> None:
        """A note with multiple derived_from_ids creates one edge per source."""
        kp = seeded_config.storage.knowledge_path
        _write_note(kp, doc_id=_ID1, title="Source-A", content="Source A")
        _write_note(kp, doc_id=_ID2, title="Source-B", content="Source B")
        _write_note(
            kp,
            doc_id=_ID3,
            title="Derived",
            content="Derived from both",
            derived_from_ids=[_ID1, _ID2],
        )
        km = KnowledgeManager(seeded_config)

        result = await _project_provenance_to_edges(edge_store, km)

        assert result["created"] == 2
        edges = await edge_store.list_edges(edge_type="derived_from")
        assert len(edges) == 2
        to_ids = {str(e["to_id"]) for e in edges}
        assert to_ids == {_ID1, _ID2}

    @pytest.mark.asyncio
    async def test_namespace_from_path(
        self, seeded_km: KnowledgeManager, edge_store: EdgeStore
    ) -> None:
        """Edge namespace is derived from the document's relative path."""
        await _project_provenance_to_edges(edge_store, seeded_km)
        edges = await edge_store.list_edges(edge_type="derived_from")
        # Gamma is in projects/ subdir → namespace = "projects"
        assert len(edges) == 1
        assert edges[0]["namespace"] == "projects"


# ---------------------------------------------------------------------------
# Test: stale edge removal
# ---------------------------------------------------------------------------


class TestStaleEdgeRemoval:
    @pytest.mark.asyncio
    async def test_removes_orphan_edges(
        self, seeded_config: LithosConfig, edge_store: EdgeStore
    ) -> None:
        """Edges for removed derived_from_ids are deleted."""
        kp = seeded_config.storage.knowledge_path
        _write_note(kp, doc_id=_ID1, title="Source", content="Source content")
        _write_note(
            kp,
            doc_id=_ID2,
            title="Child",
            content="Child content",
            derived_from_ids=[_ID1],
        )
        km = KnowledgeManager(seeded_config)

        # First projection: creates 1 edge
        r1 = await _project_provenance_to_edges(edge_store, km)
        assert r1["created"] == 1

        # Now rewrite Child without derived_from_ids
        _write_note(kp, doc_id=_ID2, title="Child", content="Child content updated")
        km2 = KnowledgeManager(seeded_config)

        # Second projection: should remove the orphan
        r2 = await _project_provenance_to_edges(edge_store, km2)
        assert r2["removed"] == 1
        assert r2["created"] == 0

        edges = await edge_store.list_edges(edge_type="derived_from")
        assert len(edges) == 0

    @pytest.mark.asyncio
    async def test_does_not_remove_non_derived_from_edges(
        self, seeded_config: LithosConfig, edge_store: EdgeStore
    ) -> None:
        """Only derived_from edges are managed; other edge types are untouched."""
        kp = seeded_config.storage.knowledge_path
        _write_note(kp, doc_id=_ID1, title="Source", content="Source")
        km = KnowledgeManager(seeded_config)

        # Insert a non-derived_from edge manually
        await edge_store.upsert(
            from_id=_ID1,
            to_id=_ID2,
            edge_type="related_to",
            weight=0.8,
            namespace="default",
        )

        result = await _project_provenance_to_edges(edge_store, km)
        assert result["created"] == 0
        assert result["removed"] == 0

        # The related_to edge must still exist
        other_edges = await edge_store.list_edges(edge_type="related_to")
        assert len(other_edges) == 1


# ---------------------------------------------------------------------------
# Test: idempotent repeat runs
# ---------------------------------------------------------------------------


class TestIdempotent:
    @pytest.mark.asyncio
    async def test_second_run_is_noop(
        self, seeded_km: KnowledgeManager, edge_store: EdgeStore
    ) -> None:
        """Running projection twice with same data creates nothing on second run."""
        r1 = await _project_provenance_to_edges(edge_store, seeded_km)
        assert r1["created"] == 1

        r2 = await _project_provenance_to_edges(edge_store, seeded_km)
        assert r2["created"] == 0
        assert r2["removed"] == 0

        edges = await edge_store.list_edges(edge_type="derived_from")
        assert len(edges) == 1

    @pytest.mark.asyncio
    async def test_edge_id_stable_across_runs(
        self, seeded_km: KnowledgeManager, edge_store: EdgeStore
    ) -> None:
        """The edge_id from first projection is preserved on subsequent runs."""
        await _project_provenance_to_edges(edge_store, seeded_km)
        edges_before = await edge_store.list_edges(edge_type="derived_from")
        edge_id_before = edges_before[0]["edge_id"]

        await _project_provenance_to_edges(edge_store, seeded_km)
        edges_after = await edge_store.list_edges(edge_type="derived_from")
        assert edges_after[0]["edge_id"] == edge_id_before


# ---------------------------------------------------------------------------
# Test: no-op when edges.db absent
# ---------------------------------------------------------------------------


class TestNoOpWhenAbsent:
    @pytest.mark.asyncio
    async def test_noop_when_edges_db_missing(
        self, seeded_config: LithosConfig, seeded_km: KnowledgeManager
    ) -> None:
        """Returns zeros and does nothing when edges.db does not exist."""
        # Do NOT call edge_store.open() — edges.db should not exist
        store = EdgeStore(seeded_config)
        assert not store.db_path.exists()

        result = await _project_provenance_to_edges(store, seeded_km)
        assert result == {"created": 0, "removed": 0}
        assert not store.db_path.exists()
