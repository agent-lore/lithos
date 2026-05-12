"""Tests for US-012: Internal provenance-to-edges projection.

Unit tests cover: forward projection, stale edge removal, idempotent
repeat runs, predicate scoping (issue #254), and no-op when edges.db
absent.

Mutation flows through the package-private plan/apply pair on
``ProvenanceProjection`` (ADR-0004), dispatched via
``KnowledgeManager.plan_reconcile`` / ``apply_reconcile``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import frontmatter as fm
import pytest

from lithos.config import LithosConfig, StorageConfig
from lithos.knowledge import KnowledgeManager
from lithos.provenance import ProvenancePlan, ProvenanceProjection, ProvenanceResult


async def _reconcile_via_km(
    km: KnowledgeManager, projection: ProvenanceProjection
) -> ProvenanceResult:
    """Plan and apply a provenance reconcile through the public KM seam.

    Used by tests that previously called the transitional
    ``projection._project(km)`` hook (removed in #254).
    """
    plan = await km.plan_reconcile(projection=projection)
    assert plan.provenance is not None
    result = await km.apply_reconcile(plan, projection=projection)
    assert result.provenance is not None
    return result.provenance


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
async def projection(seeded_config: LithosConfig):
    """Open a ProvenanceProjection (and its underlying edge store) for the test.

    Tests drive plan/apply through :func:`_reconcile_via_km` and read back
    via the public ``list_edges`` API. Edges that have no equivalent on
    the public surface (e.g. agent-asserted edges with
    ``provenance_type != 'frontmatter'`` used in predicate-scoping tests)
    are inserted via ``projection._edge_store`` directly.
    """
    proj = await ProvenanceProjection.create(seeded_config)
    try:
        yield proj
    finally:
        await proj.close()


# ---------------------------------------------------------------------------
# Test: forward projection creates derived_from edges
# ---------------------------------------------------------------------------


class TestForwardProjection:
    @pytest.mark.asyncio
    async def test_creates_derived_from_edges(
        self, seeded_km: KnowledgeManager, projection: ProvenanceProjection
    ) -> None:
        """Projection creates derived_from edge for Gamma -> Alpha."""
        result = await _reconcile_via_km(seeded_km, projection)

        assert result.created == 1
        assert result.removed == 0

        edges = await projection.list_edges(edge_type="derived_from")
        assert len(edges) == 1
        assert edges[0]["from_id"] == _ID3
        assert edges[0]["to_id"] == _ID1
        assert edges[0]["type"] == "derived_from"
        assert edges[0]["namespace"] == "projects"
        assert edges[0]["provenance_type"] == "frontmatter"

    @pytest.mark.asyncio
    async def test_multiple_sources(
        self, seeded_config: LithosConfig, projection: ProvenanceProjection
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

        result = await _reconcile_via_km(km, projection)

        assert result.created == 2
        edges = await projection.list_edges(edge_type="derived_from")
        assert len(edges) == 2
        to_ids = {str(e["to_id"]) for e in edges}
        assert to_ids == {_ID1, _ID2}

    @pytest.mark.asyncio
    async def test_namespace_from_path(
        self, seeded_km: KnowledgeManager, projection: ProvenanceProjection
    ) -> None:
        """Edge namespace is derived from the document's relative path
        when no explicit override is set in frontmatter."""
        await _reconcile_via_km(seeded_km, projection)
        edges = await projection.list_edges(edge_type="derived_from")
        # Gamma is in projects/ subdir → namespace = "projects"
        assert len(edges) == 1
        assert edges[0]["namespace"] == "projects"

    @pytest.mark.asyncio
    async def test_namespace_explicit_override_wins(
        self, seeded_config: LithosConfig, projection: ProvenanceProjection
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

        result = await _reconcile_via_km(km, projection)
        assert result.created == 1

        edges = await projection.list_edges(edge_type="derived_from")
        assert len(edges) == 1
        assert edges[0]["namespace"] == "research/alpha"


# ---------------------------------------------------------------------------
# Test: stale edge removal
# ---------------------------------------------------------------------------


class TestStaleEdgeRemoval:
    @pytest.mark.asyncio
    async def test_removes_orphan_edges(
        self, seeded_config: LithosConfig, projection: ProvenanceProjection
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
        r1 = await _reconcile_via_km(km, projection)
        assert r1.created == 1

        # Now rewrite Child without derived_from_ids
        _write_note(kp, doc_id=_ID2, title="Child", content="Child content updated")
        km2 = KnowledgeManager(seeded_config)

        # Second projection: should remove the orphan
        r2 = await _reconcile_via_km(km2, projection)
        assert r2.removed == 1
        assert r2.created == 0

        edges = await projection.list_edges(edge_type="derived_from")
        assert len(edges) == 0

    @pytest.mark.asyncio
    async def test_does_not_remove_non_derived_from_edges(
        self, seeded_config: LithosConfig, projection: ProvenanceProjection
    ) -> None:
        """Only derived_from edges are managed; other edge types are untouched."""
        kp = seeded_config.storage.knowledge_path
        _write_note(kp, doc_id=_ID1, title="Source", content="Source")
        km = KnowledgeManager(seeded_config)

        # Insert a non-derived_from edge manually
        await projection._edge_store.upsert(
            from_id=_ID1,
            to_id=_ID2,
            edge_type="related_to",
            weight=0.8,
            namespace="default",
        )

        result = await _reconcile_via_km(km, projection)
        assert result.created == 0
        assert result.removed == 0

        # The related_to edge must still exist
        other_edges = await projection.list_edges(edge_type="related_to")
        assert len(other_edges) == 1


# ---------------------------------------------------------------------------
# Test: idempotent repeat runs
# ---------------------------------------------------------------------------


class TestIdempotent:
    @pytest.mark.asyncio
    async def test_second_run_is_noop(
        self, seeded_km: KnowledgeManager, projection: ProvenanceProjection
    ) -> None:
        """Running projection twice with same data creates nothing on second run."""
        r1 = await _reconcile_via_km(seeded_km, projection)
        assert r1.created == 1

        r2 = await _reconcile_via_km(seeded_km, projection)
        assert r2.created == 0
        assert r2.removed == 0

        edges = await projection.list_edges(edge_type="derived_from")
        assert len(edges) == 1

    @pytest.mark.asyncio
    async def test_edge_id_stable_across_runs(
        self, seeded_km: KnowledgeManager, projection: ProvenanceProjection
    ) -> None:
        """The edge_id from first projection is preserved on subsequent runs."""
        await _reconcile_via_km(seeded_km, projection)
        edges_before = await projection.list_edges(edge_type="derived_from")
        edge_id_before = edges_before[0]["edge_id"]

        await _reconcile_via_km(seeded_km, projection)
        edges_after = await projection.list_edges(edge_type="derived_from")
        assert edges_after[0]["edge_id"] == edge_id_before


# ---------------------------------------------------------------------------
# Test: no-op when edges.db absent
# ---------------------------------------------------------------------------


class TestNoOpWhenAbsent:
    @pytest.mark.asyncio
    async def test_noop_when_edges_db_missing(
        self, seeded_config: LithosConfig, seeded_km: KnowledgeManager
    ) -> None:
        """plan/apply reports supported=False when edges.db does not exist."""
        # Construct the projection without ``create()`` so the underlying
        # store is *not* opened — edges.db must not exist for this test.
        proj = ProvenanceProjection(seeded_config)
        assert not proj._edge_store.db_path.exists()

        result = await _reconcile_via_km(seeded_km, proj)
        assert result.supported is False
        assert result.created == 0
        assert result.removed == 0
        assert result.actions == ()
        assert not proj._edge_store.db_path.exists()


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
        proj = await ProvenanceProjection.create(seeded_config)
        try:
            # Reference seeded_km so the fixture runs and writes notes to disk.
            _ = seeded_km

            result = await reconcile(scope="provenance_projection", config=seeded_config)

            assert result["supported"] is True
            assert result["status"] == "ok"
            assert result["summary"]["repaired"] >= 1
            # Action payload carries the (created, removed) counts.
            assert any("created" in a for a in result["actions"])

            edges = await proj.list_edges(edge_type="derived_from")
            assert len(edges) >= 1
        finally:
            await proj.close()

    @pytest.mark.asyncio
    async def test_reconcile_dry_run_reports_plan_without_mutating(
        self, seeded_config: LithosConfig, seeded_km: KnowledgeManager
    ) -> None:
        """Dry-run computes the planned create/remove counts using the
        same diff logic as a real run, but applies nothing.
        """
        from lithos.reconcile import reconcile

        proj = await ProvenanceProjection.create(seeded_config)
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
            edges = await proj.list_edges(edge_type="derived_from")
            assert len(edges) == 0
        finally:
            await proj.close()

    @pytest.mark.asyncio
    async def test_reconcile_dry_run_noop_when_already_in_sync(
        self, seeded_config: LithosConfig, seeded_km: KnowledgeManager
    ) -> None:
        """When the projection is already in sync, dry-run reports zero
        planned actions and status=noop.
        """
        from lithos.reconcile import reconcile

        proj = await ProvenanceProjection.create(seeded_config)
        try:
            _ = seeded_km

            # Apply the projection so the store is in sync.
            await reconcile(scope="provenance_projection", config=seeded_config)
            edges_after_real = await proj.list_edges(edge_type="derived_from")
            assert len(edges_after_real) == 1

            # Dry-run now plans nothing.
            result = await reconcile(
                scope="provenance_projection", dry_run=True, config=seeded_config
            )
            assert result["status"] == "noop"
            assert result["summary"]["repaired"] == 0
            assert result["actions"] == [{"created": 0, "removed": 0}]
        finally:
            await proj.close()

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


# ---------------------------------------------------------------------------
# Plan/apply pair + KnowledgeManager dispatch (issue #254 / ADR-0001 step 3)
# ---------------------------------------------------------------------------


class TestProvenancePlanApply:
    """Package-private plan/apply pair on :class:`ProvenanceProjection`.

    Covers: plan shape, predicate scoping (the central ADR-0004B
    invariant), round-trip idempotence, and dispatch via
    :class:`KnowledgeManager`.
    """

    @pytest.mark.asyncio
    async def test_plan_reports_supported_false_when_edges_db_missing(
        self, seeded_config: LithosConfig, seeded_km: KnowledgeManager
    ) -> None:
        """plan.supported is False (and noop) when edges.db is absent."""
        proj = ProvenanceProjection(seeded_config)
        assert not proj._edge_store.db_path.exists()

        plan = await seeded_km.plan_reconcile(projection=proj)
        assert plan.provenance is not None
        assert plan.provenance.supported is False
        assert plan.provenance.actions == ()
        assert plan.provenance.is_noop is True

    @pytest.mark.asyncio
    async def test_plan_is_noop_when_projection_matches_corpus(
        self, seeded_km: KnowledgeManager, projection: ProvenanceProjection
    ) -> None:
        """After applying once, re-planning produces an empty plan."""
        await _reconcile_via_km(seeded_km, projection)

        plan = await seeded_km.plan_reconcile(projection=projection)
        assert plan.provenance is not None
        assert plan.provenance.supported is True
        assert plan.provenance.is_noop is True
        assert plan.provenance.actions == ()

    @pytest.mark.asyncio
    async def test_plan_creates_action_for_new_frontmatter_link(
        self, seeded_km: KnowledgeManager, projection: ProvenanceProjection
    ) -> None:
        """A doc with ``derived_from_ids`` yields a create action."""
        plan = await seeded_km.plan_reconcile(projection=projection)
        assert plan.provenance is not None
        creates = [a for a in plan.provenance.actions if a.action == "create"]
        assert len(creates) == 1
        action = creates[0]
        assert action.target == "projection_edge"
        assert action.from_id == _ID3
        assert action.to_id == _ID1
        assert action.namespace == "projects"
        assert action.edge_id is None  # not assigned until apply

    @pytest.mark.asyncio
    async def test_plan_removes_orphan_corpus_derived_edge(
        self, seeded_km: KnowledgeManager, projection: ProvenanceProjection
    ) -> None:
        """A frontmatter-provenanced edge with no frontmatter backing it
        is planned for removal and carries its edge_id."""
        # Insert a stale frontmatter-provenanced edge (no doc derives from _ID4).
        orphan_edge_id = await projection._edge_store.upsert(
            from_id=_ID4,
            to_id=_ID1,
            edge_type="derived_from",
            weight=1.0,
            namespace="default",
            provenance_type="frontmatter",
        )

        plan = await seeded_km.plan_reconcile(projection=projection)
        assert plan.provenance is not None
        removes = [a for a in plan.provenance.actions if a.action == "remove"]
        assert len(removes) == 1
        assert removes[0].from_id == _ID4
        assert removes[0].to_id == _ID1
        assert removes[0].edge_id == orphan_edge_id

    @pytest.mark.asyncio
    async def test_plan_skips_agent_asserted_edge(
        self, seeded_km: KnowledgeManager, projection: ProvenanceProjection
    ) -> None:
        """The predicate-scoping invariant (ADR-0004B):

        An asserted edge with ``provenance_type != 'frontmatter'`` survives
        reconcile, while a stale frontmatter-provenanced edge is deleted.
        """
        # Stale frontmatter-provenanced edge (should be removed).
        await projection._edge_store.upsert(
            from_id=_ID4,
            to_id=_ID2,
            edge_type="derived_from",
            weight=1.0,
            namespace="default",
            provenance_type="frontmatter",
        )
        # Agent-asserted derived_from edge with NON-frontmatter provenance
        # (must survive reconcile — outside the predicate's scope).
        asserted_edge_id = await projection._edge_store.upsert(
            from_id=_ID2,
            to_id=_ID1,
            edge_type="derived_from",
            weight=0.5,
            namespace="default",
            provenance_type="asserted",
        )

        plan = await seeded_km.plan_reconcile(projection=projection)
        assert plan.provenance is not None
        # The only remove action targets the frontmatter-provenanced edge.
        removes = [a for a in plan.provenance.actions if a.action == "remove"]
        assert len(removes) == 1
        assert removes[0].from_id == _ID4

        result = await seeded_km.apply_reconcile(plan, projection=projection)
        assert result.provenance is not None
        assert result.provenance.removed == 1
        # The agent-asserted edge survives untouched.
        survivor = await projection.get_edge(asserted_edge_id)
        assert survivor is not None
        assert survivor["provenance_type"] == "asserted"

    @pytest.mark.asyncio
    async def test_apply_round_trip_is_idempotent(
        self, seeded_km: KnowledgeManager, projection: ProvenanceProjection
    ) -> None:
        """plan → apply → re-plan produces an empty plan; edge ids stable."""
        first = await _reconcile_via_km(seeded_km, projection)
        assert first.created == 1
        edges_before = await projection.list_edges(edge_type="derived_from")
        assert len(edges_before) == 1
        eid_before = edges_before[0]["edge_id"]

        # Second cycle: plan is noop, apply is noop, edge id unchanged.
        plan = await seeded_km.plan_reconcile(projection=projection)
        assert plan.provenance is not None
        assert plan.provenance.is_noop
        result = await seeded_km.apply_reconcile(plan, projection=projection)
        assert result.provenance is not None
        assert result.provenance.created == 0
        assert result.provenance.removed == 0

        edges_after = await projection.list_edges(edge_type="derived_from")
        assert len(edges_after) == 1
        assert edges_after[0]["edge_id"] == eid_before

    @pytest.mark.asyncio
    async def test_apply_records_failure_when_upsert_raises(
        self,
        seeded_km: KnowledgeManager,
        projection: ProvenanceProjection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing upsert is captured in result.failed, not raised."""
        plan = await seeded_km.plan_reconcile(projection=projection)
        assert plan.provenance is not None
        assert len(plan.provenance.actions) == 1

        async def _boom(**_kwargs: object) -> str:
            raise RuntimeError("simulated upsert failure")

        monkeypatch.setattr(projection._edge_store, "upsert", _boom)
        result = await projection._apply_reconcile(plan.provenance)
        assert result.created == 0
        assert len(result.failed) == 1
        assert "simulated upsert failure" in result.failed[0].detail

    @pytest.mark.asyncio
    async def test_km_plan_reconcile_populates_provenance_slice(
        self, seeded_km: KnowledgeManager, projection: ProvenanceProjection
    ) -> None:
        """plan.provenance is set when projection is passed."""
        plan = await seeded_km.plan_reconcile(projection=projection)
        assert plan.provenance is not None
        assert isinstance(plan.provenance, ProvenancePlan)

    @pytest.mark.asyncio
    async def test_km_apply_reconcile_dispatches_provenance(
        self, seeded_km: KnowledgeManager, projection: ProvenanceProjection
    ) -> None:
        """KM.apply_reconcile materialises planned edges via the projection."""
        plan = await seeded_km.plan_reconcile(projection=projection)
        result = await seeded_km.apply_reconcile(plan, projection=projection)
        assert result.provenance is not None
        assert isinstance(result.provenance, ProvenanceResult)
        assert result.provenance.created == 1
        edges = await projection.list_edges(edge_type="derived_from")
        assert len(edges) == 1

    @pytest.mark.asyncio
    async def test_km_skips_provenance_when_projection_not_passed(
        self, seeded_km: KnowledgeManager
    ) -> None:
        """plan.provenance is None when no projection argument is supplied."""
        plan = await seeded_km.plan_reconcile()
        assert plan.provenance is None

        result = await seeded_km.apply_reconcile(plan)
        assert result.provenance is None

    @pytest.mark.asyncio
    async def test_plan_emits_resync_for_stale_column_drift(
        self, seeded_km: KnowledgeManager, projection: ProvenanceProjection
    ) -> None:
        """ADR-0004 row-ownership invariant: when a frontmatter-provenanced
        row's owned columns (weight / provenance_actor / evidence /
        conflict_state) drift from canonical values, plan emits a ``resync``
        action and apply re-canonicalises the row in place.
        """
        # Seed an in-corpus frontmatter row directly with drifted columns —
        # this is the reviewer's repro: a row that the key-set diff alone
        # would treat as in-sync.
        drifted_edge_id = await projection._edge_store.upsert(
            from_id=_ID3,
            to_id=_ID1,
            edge_type="derived_from",
            weight=0.3,
            namespace="projects",
            provenance_type="frontmatter",
            evidence="stale",
            conflict_state="conflicted",
        )

        plan = await seeded_km.plan_reconcile(projection=projection)
        assert plan.provenance is not None
        resyncs = [a for a in plan.provenance.actions if a.action == "resync"]
        assert len(resyncs) == 1
        assert resyncs[0].from_id == _ID3
        assert resyncs[0].to_id == _ID1
        assert resyncs[0].namespace == "projects"
        assert resyncs[0].edge_id == drifted_edge_id

        result = await seeded_km.apply_reconcile(plan, projection=projection)
        assert result.provenance is not None
        assert result.provenance.resynced == 1
        assert result.provenance.created == 0
        assert result.provenance.removed == 0

        # Same row, now canonical.
        repaired = await projection.get_edge(drifted_edge_id)
        assert repaired is not None
        assert repaired["weight"] == 1.0
        assert repaired["provenance_actor"] is None
        assert repaired["evidence"] is None
        assert repaired["conflict_state"] is None
        assert repaired["provenance_type"] == "frontmatter"

        # And re-planning is now noop.
        plan2 = await seeded_km.plan_reconcile(projection=projection)
        assert plan2.provenance is not None
        assert plan2.provenance.is_noop

    @pytest.mark.asyncio
    async def test_plan_skips_resync_when_columns_already_canonical(
        self, seeded_km: KnowledgeManager, projection: ProvenanceProjection
    ) -> None:
        """A canonical row in sync with frontmatter emits no action.

        Guards against an "always-emit-resync" implementation that would
        churn the store on every reconcile.
        """
        await _reconcile_via_km(seeded_km, projection)

        plan = await seeded_km.plan_reconcile(projection=projection)
        assert plan.provenance is not None
        assert plan.provenance.is_noop
        assert plan.provenance.actions == ()

    @pytest.mark.asyncio
    async def test_asserted_edge_blocks_create_at_same_natural_key(
        self,
        seeded_km: KnowledgeManager,
        projection: ProvenanceProjection,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The predicate-scoping invariant must hold even when an asserted
        edge shares the natural key with a frontmatter-desired edge.

        edges.db is UNIQUE on (from_id, to_id, type, namespace), so a naive
        ``create`` via ``EdgeStore.upsert`` would update the asserted row
        in place — clobbering its provenance_type, weight, and evidence.
        Plan must detect this and emit no action; the asserted edge
        survives apply untouched. The frontmatter intent is logged as
        blocked.
        """
        # Pre-seat an asserted edge at the exact natural key the seeded
        # corpus wants (_ID3 derives from _ID1, namespace "projects").
        asserted_edge_id = await projection._edge_store.upsert(
            from_id=_ID3,
            to_id=_ID1,
            edge_type="derived_from",
            weight=0.5,
            namespace="projects",
            provenance_type="asserted",
            evidence="agent claim",
        )

        with caplog.at_level("WARNING", logger="lithos.provenance"):
            plan = await seeded_km.plan_reconcile(projection=projection)
        assert plan.provenance is not None
        assert plan.provenance.is_noop, (
            f"expected no actions (asserted row blocks create), got {plan.provenance.actions!r}"
        )
        assert any("blocked by asserted rows" in r.message for r in caplog.records)

        result = await seeded_km.apply_reconcile(plan, projection=projection)
        assert result.provenance is not None
        assert result.provenance.created == 0
        assert result.provenance.resynced == 0
        assert result.provenance.removed == 0

        # Asserted edge survives byte-for-byte.
        survivor = await projection.get_edge(asserted_edge_id)
        assert survivor is not None
        assert survivor["provenance_type"] == "asserted"
        assert survivor["weight"] == pytest.approx(0.5)
        assert survivor["evidence"] == "agent claim"
