"""Tests for LCMA stats store (stats.db)."""

import json
import sqlite3

import pytest_asyncio

from lithos.config import LithosConfig
from lithos.lcma.stats import StatsStore


@pytest_asyncio.fixture
async def stats_store(test_config: LithosConfig) -> StatsStore:
    """Create and open a StatsStore for testing."""
    store = StatsStore(test_config)
    await store.open()
    return store


class TestStatsStoreCreation:
    """DB + schema creation on first use."""

    async def test_open_creates_db_file(self, test_config: LithosConfig) -> None:
        store = StatsStore(test_config)
        assert not store.db_path.exists()
        await store.open()
        assert store.db_path.exists()

    async def test_schema_has_all_tables(self, stats_store: StatsStore) -> None:
        conn = sqlite3.connect(str(stats_store.db_path))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()
        expected = {"node_stats", "coactivation", "enrich_queue", "working_memory", "receipts"}
        assert tables == expected

    async def test_node_stats_columns(self, stats_store: StatsStore) -> None:
        conn = sqlite3.connect(str(stats_store.db_path))
        cursor = conn.execute("PRAGMA table_info(node_stats)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()
        assert columns == {
            "node_id",
            "salience",
            "retrieval_count",
            "last_retrieved_at",
            "last_used_at",
            "ignored_count",
            "misleading_count",
            "decay_rate",
            "spaced_rep_strength",
        }

    async def test_coactivation_columns(self, stats_store: StatsStore) -> None:
        conn = sqlite3.connect(str(stats_store.db_path))
        cursor = conn.execute("PRAGMA table_info(coactivation)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()
        assert columns == {"node_id_a", "node_id_b", "namespace", "count", "last_at"}

    async def test_enrich_queue_columns(self, stats_store: StatsStore) -> None:
        conn = sqlite3.connect(str(stats_store.db_path))
        cursor = conn.execute("PRAGMA table_info(enrich_queue)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()
        assert columns == {
            "id",
            "trigger_type",
            "node_id",
            "task_id",
            "triggered_at",
            "processed_at",
        }

    async def test_enrich_queue_allows_null_node_id(self, stats_store: StatsStore) -> None:
        """Task-level enrichment rows set node_id to NULL."""
        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            await db.execute(
                "INSERT INTO enrich_queue (trigger_type, node_id, task_id) VALUES (?, ?, ?)",
                ("task.completed", None, "task_abc"),
            )
            await db.commit()
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT trigger_type, node_id, task_id, processed_at FROM enrich_queue"
            )
            row = await cursor.fetchone()
        assert row is not None
        assert row["trigger_type"] == "task.completed"
        assert row["node_id"] is None
        assert row["task_id"] == "task_abc"
        assert row["processed_at"] is None

    async def test_working_memory_columns(self, stats_store: StatsStore) -> None:
        conn = sqlite3.connect(str(stats_store.db_path))
        cursor = conn.execute("PRAGMA table_info(working_memory)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()
        assert columns == {
            "task_id",
            "node_id",
            "activation_count",
            "first_seen_at",
            "last_seen_at",
            "last_receipt_id",
        }

    async def test_receipts_columns(self, stats_store: StatsStore) -> None:
        conn = sqlite3.connect(str(stats_store.db_path))
        cursor = conn.execute("PRAGMA table_info(receipts)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()
        assert columns == {
            "id",
            "ts",
            "query",
            "limit",
            "namespace_filter",
            "scouts_fired",
            "candidates_considered",
            "final_nodes",
            "conflicts_surfaced",
            "surface_conflicts",
            "temperature",
            "terrace_reached",
            "agent_id",
            "task_id",
        }

    async def test_receipts_indexes(self, stats_store: StatsStore) -> None:
        """receipts has indexes on ts, task_id, agent_id."""
        conn = sqlite3.connect(str(stats_store.db_path))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='receipts'"
        )
        indexes = {row[0] for row in cursor.fetchall()}
        conn.close()
        assert "idx_receipts_ts" in indexes
        assert "idx_receipts_task_id" in indexes
        assert "idx_receipts_agent_id" in indexes


class TestIdempotentReopen:
    """Idempotent re-open: existing stats.db preserves all rows."""

    async def test_reopen_preserves_rows(self, test_config: LithosConfig) -> None:
        store = StatsStore(test_config)
        await store.open()

        # Insert a row into node_stats
        async with __import__("aiosqlite").connect(store.db_path) as db:
            await db.execute(
                "INSERT INTO node_stats (node_id, retrieval_count, salience) VALUES (?, ?, ?)",
                ("n1", 5, 0.8),
            )
            await db.commit()

        # Re-open
        store2 = StatsStore(test_config)
        await store2.open()

        async with __import__("aiosqlite").connect(store2.db_path) as db:
            cursor = await db.execute(
                "SELECT retrieval_count, salience FROM node_stats WHERE node_id = ?", ("n1",)
            )
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 5
        assert row[1] == 0.8


class TestInsertSelectRoundTrip:
    """Insert/select round-trip for each table."""

    async def test_node_stats_round_trip(self, stats_store: StatsStore) -> None:
        import aiosqlite

        now = "2026-04-10T12:00:00Z"
        async with aiosqlite.connect(stats_store.db_path) as db:
            await db.execute(
                "INSERT INTO node_stats (node_id, retrieval_count, last_retrieved_at, salience) "
                "VALUES (?, ?, ?, ?)",
                ("node_1", 3, now, 0.75),
            )
            await db.commit()

        async with aiosqlite.connect(stats_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM node_stats WHERE node_id = ?", ("node_1",))
            row = await cursor.fetchone()
        assert row is not None
        assert row["node_id"] == "node_1"
        assert row["retrieval_count"] == 3
        assert row["last_retrieved_at"] == now
        assert row["salience"] == 0.75

    async def test_coactivation_round_trip(self, stats_store: StatsStore) -> None:
        import aiosqlite

        now = "2026-04-10T12:00:00Z"
        async with aiosqlite.connect(stats_store.db_path) as db:
            await db.execute(
                "INSERT INTO coactivation "
                "(node_id_a, node_id_b, namespace, count, last_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("a", "b", "default", 7, now),
            )
            await db.commit()

        async with aiosqlite.connect(stats_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM coactivation WHERE node_id_a = ? AND node_id_b = ?", ("a", "b")
            )
            row = await cursor.fetchone()
        assert row is not None
        assert row["namespace"] == "default"
        assert row["count"] == 7
        assert row["last_at"] == now

    async def test_enrich_queue_round_trip(self, stats_store: StatsStore) -> None:
        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            # id is INTEGER AUTOINCREMENT — do not bind it
            cursor = await db.execute(
                "INSERT INTO enrich_queue (trigger_type, node_id) VALUES (?, ?)",
                ("note.created", "node_1"),
            )
            inserted_id = cursor.lastrowid
            await db.commit()

        async with aiosqlite.connect(stats_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM enrich_queue WHERE id = ?", (inserted_id,))
            row = await cursor.fetchone()
        assert row is not None
        assert row["node_id"] == "node_1"
        assert row["trigger_type"] == "note.created"
        assert row["task_id"] is None
        assert row["processed_at"] is None
        assert row["triggered_at"] is not None

    async def test_working_memory_round_trip(self, stats_store: StatsStore) -> None:
        import aiosqlite

        now = "2026-04-10T12:00:00Z"
        async with aiosqlite.connect(stats_store.db_path) as db:
            await db.execute(
                "INSERT INTO working_memory "
                "(task_id, node_id, activation_count, first_seen_at, "
                " last_seen_at, last_receipt_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("task_1", "node_1", 2, now, now, "rcpt_abc123"),
            )
            await db.commit()

        async with aiosqlite.connect(stats_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM working_memory WHERE task_id = ? AND node_id = ?",
                ("task_1", "node_1"),
            )
            row = await cursor.fetchone()
        assert row is not None
        assert row["activation_count"] == 2
        assert row["first_seen_at"] == now
        assert row["last_seen_at"] == now
        assert row["last_receipt_id"] == "rcpt_abc123"

    async def test_receipts_round_trip(self, stats_store: StatsStore) -> None:
        import aiosqlite

        scouts = json.dumps(["scout_vector", "scout_lexical"])
        nodes = json.dumps(["n1", "n2"])
        conflicts = json.dumps([])
        now = "2026-04-10T12:00:00Z"

        async with aiosqlite.connect(stats_store.db_path) as db:
            await db.execute(
                "INSERT INTO receipts "
                '(id, ts, query, "limit", namespace_filter, scouts_fired, '
                " candidates_considered, final_nodes, conflicts_surfaced, "
                " surface_conflicts, temperature, terrace_reached, "
                " agent_id, task_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "rcpt_test1",
                    now,
                    "test query",
                    10,
                    None,
                    scouts,
                    42,
                    nodes,
                    conflicts,
                    1,
                    0.5,
                    1,
                    "agent_1",
                    "task_1",
                ),
            )
            await db.commit()

        async with aiosqlite.connect(stats_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM receipts WHERE id = ?", ("rcpt_test1",))
            row = await cursor.fetchone()
        assert row is not None
        assert row["query"] == "test query"
        assert row["limit"] == 10
        assert row["namespace_filter"] is None  # SQL NULL when None
        assert json.loads(row["scouts_fired"]) == ["scout_vector", "scout_lexical"]
        assert row["candidates_considered"] == 42
        assert json.loads(row["final_nodes"]) == ["n1", "n2"]
        assert json.loads(row["conflicts_surfaced"]) == []
        assert row["surface_conflicts"] == 1
        assert row["temperature"] == 0.5
        assert row["terrace_reached"] == 1
        assert row["agent_id"] == "agent_1"
        assert row["task_id"] == "task_1"

    async def test_receipts_namespace_filter_none(self, stats_store: StatsStore) -> None:
        """namespace_filter is SQL NULL when None."""
        import aiosqlite

        async with aiosqlite.connect(stats_store.db_path) as db:
            await db.execute(
                "INSERT INTO receipts "
                '(id, query, "limit", namespace_filter, scouts_fired, final_nodes, '
                "conflicts_surfaced, temperature, terrace_reached) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("rcpt_null", "q", 5, None, "[]", "[]", "[]", 0.5, 0),
            )
            await db.commit()

        async with aiosqlite.connect(stats_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT namespace_filter FROM receipts WHERE id = ?", ("rcpt_null",)
            )
            row = await cursor.fetchone()
        assert row is not None
        assert row["namespace_filter"] is None

    async def test_receipts_namespace_filter_list(self, stats_store: StatsStore) -> None:
        """namespace_filter is a JSON array string when provided."""
        import aiosqlite

        ns_filter = json.dumps(["ns1", "ns2"])
        async with aiosqlite.connect(stats_store.db_path) as db:
            await db.execute(
                "INSERT INTO receipts "
                '(id, query, "limit", namespace_filter, scouts_fired, final_nodes, '
                "conflicts_surfaced, temperature, terrace_reached) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("rcpt_list", "q", 5, ns_filter, "[]", "[]", "[]", 0.5, 0),
            )
            await db.commit()

        async with aiosqlite.connect(stats_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT namespace_filter FROM receipts WHERE id = ?", ("rcpt_list",)
            )
            row = await cursor.fetchone()
        assert row is not None
        assert json.loads(row["namespace_filter"]) == ["ns1", "ns2"]


class TestCorruptRecovery:
    """Corrupt stats.db is quarantined and recreated."""

    async def test_corrupt_db_is_quarantined(self, test_config: LithosConfig) -> None:
        store = StatsStore(test_config)
        store.db_path.parent.mkdir(parents=True, exist_ok=True)
        store.db_path.write_bytes(b"not a sqlite database at all")

        await store.open()
        assert store.db_path.exists()
        quarantined = list(store.db_path.parent.glob("stats.db.corrupt-*"))
        assert len(quarantined) == 1

    async def test_quarantined_db_contains_original_bytes(self, test_config: LithosConfig) -> None:
        store = StatsStore(test_config)
        store.db_path.parent.mkdir(parents=True, exist_ok=True)
        garbage = b"corrupt data 12345"
        store.db_path.write_bytes(garbage)

        await store.open()
        quarantined = list(store.db_path.parent.glob("stats.db.corrupt-*"))
        assert quarantined[0].read_bytes() == garbage

    async def test_recreated_db_has_all_tables(self, test_config: LithosConfig) -> None:
        store = StatsStore(test_config)
        store.db_path.parent.mkdir(parents=True, exist_ok=True)
        store.db_path.write_bytes(b"garbage")

        await store.open()
        conn = sqlite3.connect(str(store.db_path))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()
        assert tables == {
            "node_stats",
            "coactivation",
            "enrich_queue",
            "working_memory",
            "receipts",
        }


class TestStoreLocation:
    """Store location respects LithosConfig.storage.data_dir."""

    async def test_db_path_under_data_dir(self, test_config: LithosConfig) -> None:
        store = StatsStore(test_config)
        expected = test_config.storage.data_dir / ".lithos" / "stats.db"
        assert store.db_path == expected
