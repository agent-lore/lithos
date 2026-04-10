"""Tests for LCMA edge store (edges.db)."""

import sqlite3

import pytest
import pytest_asyncio

from lithos.config import LithosConfig
from lithos.lcma.edges import EdgeStore


@pytest_asyncio.fixture
async def edge_store(test_config: LithosConfig) -> EdgeStore:
    """Create and open an EdgeStore for testing."""
    store = EdgeStore(test_config)
    await store.open()
    return store


class TestEdgeStoreCreation:
    """DB + schema creation on first use."""

    async def test_open_creates_db_file(self, test_config: LithosConfig) -> None:
        store = EdgeStore(test_config)
        assert not store.db_path.exists()
        await store.open()
        assert store.db_path.exists()

    async def test_schema_has_edges_table(self, edge_store: EdgeStore) -> None:
        conn = sqlite3.connect(str(edge_store.db_path))
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='edges'")
        assert cursor.fetchone() is not None
        conn.close()

    async def test_schema_has_expected_columns(self, edge_store: EdgeStore) -> None:
        conn = sqlite3.connect(str(edge_store.db_path))
        cursor = conn.execute("PRAGMA table_info(edges)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()
        expected = {
            "edge_id",
            "from_id",
            "to_id",
            "type",
            "weight",
            "namespace",
            "created_at",
            "updated_at",
            "provenance_actor",
            "provenance_type",
            "evidence",
            "conflict_state",
        }
        assert columns == expected

    async def test_schema_has_indexes(self, edge_store: EdgeStore) -> None:
        conn = sqlite3.connect(str(edge_store.db_path))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_edges_%'"
        )
        indexes = {row[0] for row in cursor.fetchall()}
        conn.close()
        assert indexes == {
            "idx_edges_from_id",
            "idx_edges_to_id",
            "idx_edges_type",
            "idx_edges_namespace",
        }

    async def test_schema_has_unique_constraint(self, edge_store: EdgeStore) -> None:
        conn = sqlite3.connect(str(edge_store.db_path))
        # Insert a row, then try duplicate composite key
        conn.execute(
            "INSERT INTO edges (edge_id, from_id, to_id, type, weight, namespace) "
            "VALUES ('e1', 'a', 'b', 'rel', 1.0, 'ns')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO edges (edge_id, from_id, to_id, type, weight, namespace) "
                "VALUES ('e2', 'a', 'b', 'rel', 1.0, 'ns')"
            )
        conn.close()


class TestIdempotentReopen:
    """Idempotent re-open: existing edges.db preserves all rows."""

    async def test_reopen_preserves_rows(self, test_config: LithosConfig) -> None:
        store = EdgeStore(test_config)
        await store.open()
        edge_id = await store.upsert(
            from_id="n1",
            to_id="n2",
            edge_type="derived_from",
            weight=1.0,
            namespace="default",
        )

        # Re-open
        store2 = EdgeStore(test_config)
        await store2.open()
        edges = await store2.list_edges(from_id="n1")
        assert len(edges) == 1
        assert edges[0]["edge_id"] == edge_id


class TestNoopWhenExists:
    """No-op when DB exists — open does not drop or recreate rows."""

    async def test_open_does_not_drop_data(self, test_config: LithosConfig) -> None:
        store = EdgeStore(test_config)
        await store.open()

        # Insert multiple edges
        await store.upsert(from_id="a", to_id="b", edge_type="t1", weight=1.0, namespace="ns")
        await store.upsert(from_id="c", to_id="d", edge_type="t2", weight=0.5, namespace="ns")
        assert await store.count() == 2

        # Re-open and verify
        await store.open()
        assert await store.count() == 2


class TestCorruptRecovery:
    """Corrupt edges.db is quarantined and recreated."""

    async def test_corrupt_db_is_quarantined(self, test_config: LithosConfig) -> None:
        store = EdgeStore(test_config)
        # Write garbage to the DB path
        store.db_path.parent.mkdir(parents=True, exist_ok=True)
        store.db_path.write_bytes(b"not a sqlite database at all")

        await store.open()
        # DB should now be healthy
        assert store.db_path.exists()
        # Quarantine file should exist
        quarantined = list(store.db_path.parent.glob("edges.db.corrupt-*"))
        assert len(quarantined) == 1

    async def test_quarantined_db_contains_original_bytes(self, test_config: LithosConfig) -> None:
        store = EdgeStore(test_config)
        store.db_path.parent.mkdir(parents=True, exist_ok=True)
        garbage = b"corrupt data 12345"
        store.db_path.write_bytes(garbage)

        await store.open()
        quarantined = list(store.db_path.parent.glob("edges.db.corrupt-*"))
        assert quarantined[0].read_bytes() == garbage

    async def test_recreated_db_has_schema(self, test_config: LithosConfig) -> None:
        store = EdgeStore(test_config)
        store.db_path.parent.mkdir(parents=True, exist_ok=True)
        store.db_path.write_bytes(b"garbage")

        await store.open()
        # Should be able to upsert into the fresh DB
        edge_id = await store.upsert(
            from_id="x",
            to_id="y",
            edge_type="test",
            weight=1.0,
            namespace="default",
        )
        assert edge_id.startswith("edge_")


class TestStoreLocation:
    """Store location respects LithosConfig.storage.data_dir."""

    async def test_db_path_under_data_dir(self, test_config: LithosConfig) -> None:
        store = EdgeStore(test_config)
        expected = test_config.storage.data_dir / ".lithos" / "edges.db"
        assert store.db_path == expected


class TestUpsertAndList:
    """Basic upsert/list operations (further coverage in US-010)."""

    async def test_upsert_returns_edge_id(self, edge_store: EdgeStore) -> None:
        eid = await edge_store.upsert(
            from_id="a",
            to_id="b",
            edge_type="rel",
            weight=1.0,
            namespace="ns",
        )
        assert eid.startswith("edge_")

    async def test_upsert_same_key_updates(self, edge_store: EdgeStore) -> None:
        eid1 = await edge_store.upsert(
            from_id="a",
            to_id="b",
            edge_type="rel",
            weight=1.0,
            namespace="ns",
        )
        eid2 = await edge_store.upsert(
            from_id="a",
            to_id="b",
            edge_type="rel",
            weight=0.5,
            namespace="ns",
        )
        assert eid1 == eid2
        edges = await edge_store.list_edges(from_id="a")
        assert len(edges) == 1
        assert edges[0]["weight"] == 0.5

    async def test_list_filters(self, edge_store: EdgeStore) -> None:
        await edge_store.upsert(from_id="a", to_id="b", edge_type="t1", weight=1.0, namespace="ns1")
        await edge_store.upsert(from_id="a", to_id="c", edge_type="t2", weight=1.0, namespace="ns2")

        assert len(await edge_store.list_edges(from_id="a")) == 2
        assert len(await edge_store.list_edges(namespace="ns1")) == 1
        assert len(await edge_store.list_edges(edge_type="t2")) == 1
        assert len(await edge_store.list_edges(to_id="b")) == 1

    async def test_count(self, edge_store: EdgeStore) -> None:
        assert await edge_store.count() == 0
        await edge_store.upsert(from_id="a", to_id="b", edge_type="r", weight=1.0, namespace="ns")
        assert await edge_store.count() == 1
        assert await edge_store.count(namespace="ns") == 1
        assert await edge_store.count(namespace="other") == 0
