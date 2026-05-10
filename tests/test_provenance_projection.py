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
async def edge_store(seeded_config: LithosConfig):
    store = EdgeStore(seeded_config)
    await store.open()
    try:
        yield store
    finally:
        await store.close()


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
        """Edge namespace is derived from the document's relative path
        when no explicit override is set in frontmatter."""
        await _project_provenance_to_edges(edge_store, seeded_km)
        edges = await edge_store.list_edges(edge_type="derived_from")
        # Gamma is in projects/ subdir → namespace = "projects"
        assert len(edges) == 1
        assert edges[0]["namespace"] == "projects"

    @pytest.mark.asyncio
    async def test_namespace_explicit_override_wins(
        self, seeded_config: LithosConfig, edge_store: EdgeStore
    ) -> None:
        """An explicit ``namespace`` in frontmatter must override path
        derivation when projecting derived_from edges.
        """
        kp = seeded_config.storage.knowledge_path
        _write_note(kp, doc_id=_ID1, title="Source", content="Source content")

        # Note 2 lives in projects/ (path-derived namespace = "projects")
        # but explicitly overrides to "research/alpha".
        now = datetime.now(timezone.utc).isoformat()
        post = fm.Post(
            "Override-namespaced derivation",
            id=_ID2,
            title="Derived Override",
            author="test",
            created_at=now,
            updated_at=now,
            tags=["test"],
            access_scope="shared",
            derived_from_ids=[_ID1],
            namespace="research/alpha",
        )
        target = kp / "projects"
        target.mkdir(parents=True, exist_ok=True)
        (target / "derived-override.md").write_text(fm.dumps(post))

        km = KnowledgeManager(seeded_config)

        result = await _project_provenance_to_edges(edge_store, km)
        assert result["created"] == 1

        edges = await edge_store.list_edges(edge_type="derived_from")
        assert len(edges) == 1
        assert edges[0]["namespace"] == "research/alpha"


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


# ---------------------------------------------------------------------------
# Test: reconcile wire-up invokes the real projection
# ---------------------------------------------------------------------------


class TestReconcileWireUp:
    @pytest.mark.asyncio
    async def test_reconcile_provenance_projection_runs_real_logic(
        self, seeded_config: LithosConfig, seeded_km: KnowledgeManager
    ) -> None:
        """reconcile(scope='provenance_projection') creates real edges.

        Regression test — before the MVP 1 cleanup this call returned
        supported=True/noop/reason=not_implemented without touching edges.db.
        """
        from lithos.reconcile import reconcile

        # Ensure edges.db exists (and is empty) so the reconcile function
        # considers provenance_projection supported.
        store = EdgeStore(seeded_config)
        await store.open()
        try:
            # Reference seeded_km so the fixture runs and writes notes to disk.
            _ = seeded_km

            result = await reconcile(scope="provenance_projection", config=seeded_config)

            assert result["supported"] is True
            assert result["status"] == "ok"
            assert result["summary"]["repaired"] >= 1
            # Action payload carries the (created, removed) counts.
            assert any("created" in a for a in result["actions"])

            edges = await store.list_edges(edge_type="derived_from")
            assert len(edges) >= 1
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_reconcile_dry_run_reports_plan_without_mutating(
        self, seeded_config: LithosConfig, seeded_km: KnowledgeManager
    ) -> None:
        """Dry-run computes the planned create/remove counts using the
        same diff logic as a real run, but applies nothing.
        """
        from lithos.reconcile import reconcile

        store = EdgeStore(seeded_config)
        await store.open()
        try:
            _ = seeded_km  # seed Alpha/Beta/Gamma; Gamma derives from Alpha

            result = await reconcile(
                scope="provenance_projection", dry_run=True, config=seeded_config
            )
            assert result["supported"] is True
            # Dry-run reports the planned non-zero delta — status is "ok"
            # because there is real work the run would have done.
            assert result["status"] == "ok"
            assert result["summary"]["repaired"] == 1
            assert result["actions"] == [{"created": 1, "removed": 0}]

            # No edges were actually written.
            edges = await store.list_edges(edge_type="derived_from")
            assert len(edges) == 0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_reconcile_dry_run_noop_when_already_in_sync(
        self, seeded_config: LithosConfig, seeded_km: KnowledgeManager
    ) -> None:
        """When the projection is already in sync, dry-run reports zero
        planned actions and status=noop.
        """
        from lithos.reconcile import reconcile

        store = EdgeStore(seeded_config)
        await store.open()
        try:
            _ = seeded_km

            # Apply the projection so the store is in sync.
            await reconcile(scope="provenance_projection", config=seeded_config)
            edges_after_real = await store.list_edges(edge_type="derived_from")
            assert len(edges_after_real) == 1

            # Dry-run now plans nothing.
            result = await reconcile(
                scope="provenance_projection", dry_run=True, config=seeded_config
            )
            assert result["status"] == "noop"
            assert result["summary"]["repaired"] == 0
            assert result["actions"] == [{"created": 0, "removed": 0}]
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_reconcile_unsupported_when_edges_db_missing(
        self, seeded_config: LithosConfig, seeded_km: KnowledgeManager
    ) -> None:
        """When edges.db does not exist, supported=False and no action taken."""
        from lithos.reconcile import reconcile

        _ = seeded_km
        edges_db = seeded_config.storage.data_dir / ".lithos" / "edges.db"
        assert not edges_db.exists()

        result = await reconcile(scope="provenance_projection", config=seeded_config)
        assert result["supported"] is False
        assert result["status"] == "noop"


# ---------------------------------------------------------------------------
# ProvenanceProjection Module facade (issue #251 / ADR-0004)
#
# These tests exercise the public read surface and lifecycle of the
# ``ProvenanceProjection`` Module. Mutation is performed against the
# internal store because the projection's public surface in this slice is
# read-only — plan/apply land in #254 per ADR-0001 step 3.
# ---------------------------------------------------------------------------


import pytest_asyncio  # noqa: E402

from lithos.provenance import ProvenanceProjection  # noqa: E402


@pytest_asyncio.fixture
async def projection(test_config: LithosConfig):
    """Open a ProvenanceProjection for the test and close it on teardown."""
    proj = await ProvenanceProjection.create(test_config)
    try:
        yield proj
    finally:
        await proj.close()


class TestProvenanceProjectionCreate:
    """Eager-init factory opens the underlying store before returning."""

    async def test_create_opens_store(self, test_config: LithosConfig) -> None:
        proj = await ProvenanceProjection.create(test_config)
        try:
            # If create did not open the store, the first read would
            # hit the assert in EdgeStore._conn().
            assert await proj.count() == 0
        finally:
            await proj.close()

    async def test_create_uses_supplied_config(self, test_config: LithosConfig) -> None:
        """Edges written through the projection land at the configured path."""
        proj = await ProvenanceProjection.create(test_config)
        try:
            await proj._edge_store.upsert(
                from_id="a",
                to_id="b",
                edge_type="rel",
                weight=1.0,
                namespace="ns",
            )
            assert proj._edge_store.db_path == test_config.storage.edges_db_path
            assert proj._edge_store.db_path.exists()
        finally:
            await proj.close()


class TestProvenanceProjectionLifecycle:
    """close is idempotent and a fresh create reopens the store."""

    async def test_close_is_idempotent(self, test_config: LithosConfig) -> None:
        proj = await ProvenanceProjection.create(test_config)
        await proj.close()
        # A second close must not raise (matches EdgeStore.close contract).
        await proj.close()

    async def test_reuse_after_close_via_create(self, test_config: LithosConfig) -> None:
        first = await ProvenanceProjection.create(test_config)
        await first._edge_store.upsert(
            from_id="a",
            to_id="b",
            edge_type="rel",
            weight=0.5,
            namespace="ns",
        )
        await first.close()

        second = await ProvenanceProjection.create(test_config)
        try:
            edges = await second.list_edges(from_id="a")
            assert len(edges) == 1
            assert edges[0]["weight"] == 0.5
        finally:
            await second.close()


class TestProvenanceProjectionListEdges:
    """list_edges filters by from_id, to_id, edge_type, and namespace."""

    async def test_no_edges_returns_empty(self, projection: ProvenanceProjection) -> None:
        assert await projection.list_edges() == []

    async def test_filter_by_from_id(self, projection: ProvenanceProjection) -> None:
        await projection._edge_store.upsert(
            from_id="a", to_id="b", edge_type="rel", weight=1.0, namespace="ns"
        )
        await projection._edge_store.upsert(
            from_id="x", to_id="y", edge_type="rel", weight=1.0, namespace="ns"
        )
        rows = await projection.list_edges(from_id="a")
        assert len(rows) == 1
        assert rows[0]["from_id"] == "a"
        assert rows[0]["to_id"] == "b"

    async def test_filter_by_to_id(self, projection: ProvenanceProjection) -> None:
        await projection._edge_store.upsert(
            from_id="a", to_id="b", edge_type="rel", weight=1.0, namespace="ns"
        )
        await projection._edge_store.upsert(
            from_id="x", to_id="y", edge_type="rel", weight=1.0, namespace="ns"
        )
        rows = await projection.list_edges(to_id="b")
        assert len(rows) == 1
        assert rows[0]["to_id"] == "b"

    async def test_filter_by_edge_type(self, projection: ProvenanceProjection) -> None:
        await projection._edge_store.upsert(
            from_id="a", to_id="b", edge_type="t1", weight=1.0, namespace="ns"
        )
        await projection._edge_store.upsert(
            from_id="a", to_id="c", edge_type="t2", weight=1.0, namespace="ns"
        )
        rows = await projection.list_edges(edge_type="t2")
        assert len(rows) == 1
        assert rows[0]["type"] == "t2"

    async def test_filter_by_namespace(self, projection: ProvenanceProjection) -> None:
        await projection._edge_store.upsert(
            from_id="a", to_id="b", edge_type="rel", weight=1.0, namespace="ns1"
        )
        await projection._edge_store.upsert(
            from_id="a", to_id="c", edge_type="rel", weight=1.0, namespace="ns2"
        )
        rows = await projection.list_edges(namespace="ns1")
        assert len(rows) == 1
        assert rows[0]["namespace"] == "ns1"


class TestProvenanceProjectionGetEdge:
    """get_edge returns the edge dict or None."""

    async def test_returns_dict_for_existing_edge(self, projection: ProvenanceProjection) -> None:
        eid = await projection._edge_store.upsert(
            from_id="a",
            to_id="b",
            edge_type="rel",
            weight=0.7,
            namespace="ns",
        )
        edge = await projection.get_edge(eid)
        assert edge is not None
        assert edge["edge_id"] == eid
        assert edge["from_id"] == "a"
        assert edge["to_id"] == "b"
        assert edge["weight"] == pytest.approx(0.7)

    async def test_returns_none_for_unknown_edge(self, projection: ProvenanceProjection) -> None:
        assert await projection.get_edge("edge_does_not_exist") is None


class TestProvenanceProjectionCount:
    """count returns total rows or per-namespace rows."""

    async def test_count_total(self, projection: ProvenanceProjection) -> None:
        assert await projection.count() == 0
        await projection._edge_store.upsert(
            from_id="a", to_id="b", edge_type="rel", weight=1.0, namespace="ns"
        )
        assert await projection.count() == 1

    async def test_count_per_namespace(self, projection: ProvenanceProjection) -> None:
        await projection._edge_store.upsert(
            from_id="a", to_id="b", edge_type="rel", weight=1.0, namespace="ns1"
        )
        await projection._edge_store.upsert(
            from_id="x", to_id="y", edge_type="rel", weight=1.0, namespace="ns2"
        )
        assert await projection.count(namespace="ns1") == 1
        assert await projection.count(namespace="ns2") == 1
        assert await projection.count(namespace="absent") == 0


class TestProvenanceProjectionListEdgesBetween:
    """list_edges_between restricts to edges with both endpoints in the supplied set."""

    async def test_only_returns_edges_between_supplied_nodes(
        self, projection: ProvenanceProjection
    ) -> None:
        await projection._edge_store.upsert(
            from_id="a", to_id="b", edge_type="rel", weight=1.0, namespace="ns"
        )
        # b -> c has only one endpoint (b) in the set, so it must be excluded.
        await projection._edge_store.upsert(
            from_id="b", to_id="c", edge_type="rel", weight=1.0, namespace="ns"
        )
        rows = await projection.list_edges_between(["a", "b"])
        assert len(rows) == 1
        assert {rows[0]["from_id"], rows[0]["to_id"]} == {"a", "b"}

    async def test_filter_by_edge_type(self, projection: ProvenanceProjection) -> None:
        await projection._edge_store.upsert(
            from_id="a", to_id="b", edge_type="t1", weight=1.0, namespace="ns"
        )
        await projection._edge_store.upsert(
            from_id="a", to_id="b", edge_type="t2", weight=1.0, namespace="ns"
        )
        rows = await projection.list_edges_between(["a", "b"], edge_type="t1")
        assert len(rows) == 1
        assert rows[0]["type"] == "t1"
