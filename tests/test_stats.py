"""Tests for LCMA stats store (stats.db)."""

import asyncio
import json
import sqlite3

import pytest
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
        expected = {
            "node_stats",
            "coactivation",
            "enrich_queue",
            "working_memory",
            "receipts",
            "task_consolidation_log",
            "consolidation_edge_ops",
            "consolidation_salience_ops",
        }
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
            "cited_count",
            "last_decay_applied_at",
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
            "attempts",
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
            "task_consolidation_log",
            "consolidation_edge_ops",
            "consolidation_salience_ops",
        }


class TestStoreLocation:
    """Store location respects LithosConfig.storage.data_dir."""

    async def test_db_path_under_data_dir(self, test_config: LithosConfig) -> None:
        store = StatsStore(test_config)
        expected = test_config.storage.data_dir / ".lithos" / "stats.db"
        assert store.db_path == expected


class TestGetNodeStats:
    """get_node_stats returns dict or None."""

    async def test_returns_none_for_absent_node(self, stats_store: StatsStore) -> None:
        result = await stats_store.get_node_stats("nonexistent")
        assert result is None

    async def test_returns_dict_for_existing_node(self, stats_store: StatsStore) -> None:
        await stats_store.increment_node_stats(node_id="n1")
        result = await stats_store.get_node_stats("n1")
        assert result is not None
        assert result["node_id"] == "n1"
        assert result["retrieval_count"] == 1
        assert result["salience"] == 0.5
        assert result["ignored_count"] == 0
        assert result["cited_count"] == 0
        assert result["misleading_count"] == 0


class TestUpdateSalience:
    """update_salience: insert-on-absent, update-on-existing, clamping."""

    async def test_creates_row_if_absent(self, stats_store: StatsStore) -> None:
        await stats_store.update_salience("n1", 0.1)
        row = await stats_store.get_node_stats("n1")
        assert row is not None
        assert row["salience"] == pytest.approx(0.6)

    async def test_updates_existing_row(self, stats_store: StatsStore) -> None:
        await stats_store.update_salience("n1", 0.0)  # creates at 0.5
        await stats_store.update_salience("n1", 0.2)
        row = await stats_store.get_node_stats("n1")
        assert row is not None
        assert row["salience"] == pytest.approx(0.7)

    async def test_clamps_to_upper_bound(self, stats_store: StatsStore) -> None:
        await stats_store.update_salience("n1", 0.0)  # 0.5
        await stats_store.update_salience("n1", 10.0)
        row = await stats_store.get_node_stats("n1")
        assert row is not None
        assert row["salience"] == pytest.approx(1.0)

    async def test_clamps_to_lower_bound(self, stats_store: StatsStore) -> None:
        await stats_store.update_salience("n1", 0.0)  # 0.5
        await stats_store.update_salience("n1", -10.0)
        row = await stats_store.get_node_stats("n1")
        assert row is not None
        assert row["salience"] == pytest.approx(0.0)

    async def test_insert_clamps_initial_value(self, stats_store: StatsStore) -> None:
        await stats_store.update_salience("n1", 2.0)  # 0.5 + 2.0 = 2.5 → clamped to 1.0
        row = await stats_store.get_node_stats("n1")
        assert row is not None
        assert row["salience"] == pytest.approx(1.0)


class TestIncrementIgnored:
    """increment_ignored: insert-on-absent, update-on-existing."""

    async def test_creates_row_if_absent(self, stats_store: StatsStore) -> None:
        await stats_store.increment_ignored("n1")
        row = await stats_store.get_node_stats("n1")
        assert row is not None
        assert row["ignored_count"] == 1

    async def test_increments_existing(self, stats_store: StatsStore) -> None:
        await stats_store.increment_ignored("n1")
        await stats_store.increment_ignored("n1")
        row = await stats_store.get_node_stats("n1")
        assert row is not None
        assert row["ignored_count"] == 2


class TestIncrementCited:
    """increment_cited: insert-on-absent, update-on-existing."""

    async def test_creates_row_if_absent(self, stats_store: StatsStore) -> None:
        await stats_store.increment_cited("n1")
        row = await stats_store.get_node_stats("n1")
        assert row is not None
        assert row["cited_count"] == 1

    async def test_increments_existing(self, stats_store: StatsStore) -> None:
        await stats_store.increment_cited("n1")
        await stats_store.increment_cited("n1")
        await stats_store.increment_cited("n1")
        row = await stats_store.get_node_stats("n1")
        assert row is not None
        assert row["cited_count"] == 3


class TestIncrementMisleading:
    """increment_misleading: insert-on-absent, update-on-existing."""

    async def test_creates_row_if_absent(self, stats_store: StatsStore) -> None:
        await stats_store.increment_misleading("n1")
        row = await stats_store.get_node_stats("n1")
        assert row is not None
        assert row["misleading_count"] == 1

    async def test_increments_existing(self, stats_store: StatsStore) -> None:
        await stats_store.increment_misleading("n1")
        await stats_store.increment_misleading("n1")
        row = await stats_store.get_node_stats("n1")
        assert row is not None
        assert row["misleading_count"] == 2


class TestUpdateSpacedRepStrength:
    """update_spaced_rep_strength: insert-on-absent, clamping."""

    async def test_creates_row_if_absent(self, stats_store: StatsStore) -> None:
        await stats_store.update_spaced_rep_strength("n1", 0.3)
        row = await stats_store.get_node_stats("n1")
        assert row is not None
        assert row["spaced_rep_strength"] == pytest.approx(0.3)

    async def test_updates_existing_row(self, stats_store: StatsStore) -> None:
        await stats_store.update_spaced_rep_strength("n1", 0.3)
        await stats_store.update_spaced_rep_strength("n1", 0.2)
        row = await stats_store.get_node_stats("n1")
        assert row is not None
        assert row["spaced_rep_strength"] == pytest.approx(0.5)

    async def test_clamps_to_upper_bound(self, stats_store: StatsStore) -> None:
        await stats_store.update_spaced_rep_strength("n1", 0.5)
        await stats_store.update_spaced_rep_strength("n1", 10.0)
        row = await stats_store.get_node_stats("n1")
        assert row is not None
        assert row["spaced_rep_strength"] == pytest.approx(1.0)

    async def test_clamps_to_lower_bound(self, stats_store: StatsStore) -> None:
        await stats_store.update_spaced_rep_strength("n1", 0.5)
        await stats_store.update_spaced_rep_strength("n1", -10.0)
        row = await stats_store.get_node_stats("n1")
        assert row is not None
        assert row["spaced_rep_strength"] == pytest.approx(0.0)


class TestCitedCountMigration:
    """ALTER TABLE migration adds cited_count to existing databases."""

    async def test_migration_adds_cited_count_to_old_db(self, test_config: LithosConfig) -> None:
        """Simulate an old database without cited_count and verify migration."""
        import aiosqlite

        path = test_config.storage.stats_db_path
        path.parent.mkdir(parents=True, exist_ok=True)

        # Create old schema without cited_count
        async with aiosqlite.connect(path) as db:
            await db.execute(
                """CREATE TABLE IF NOT EXISTS node_stats (
                    node_id TEXT PRIMARY KEY,
                    salience REAL NOT NULL DEFAULT 0.5,
                    retrieval_count INTEGER NOT NULL DEFAULT 0,
                    last_retrieved_at TIMESTAMP,
                    last_used_at TIMESTAMP,
                    ignored_count INTEGER NOT NULL DEFAULT 0,
                    misleading_count INTEGER NOT NULL DEFAULT 0,
                    decay_rate REAL NOT NULL DEFAULT 0.0,
                    spaced_rep_strength REAL NOT NULL DEFAULT 0.0
                )"""
            )
            await db.execute("INSERT INTO node_stats (node_id) VALUES (?)", ("old_node",))
            await db.commit()

        # Open via StatsStore — should run migration
        store = StatsStore(test_config)
        await store.open()

        row = await store.get_node_stats("old_node")
        assert row is not None
        assert row["cited_count"] == 0


class TestGetWorkingMemory:
    """get_working_memory returns rows ordered by activation_count descending."""

    async def test_returns_empty_list_for_unknown_task(self, stats_store: StatsStore) -> None:
        result = await stats_store.get_working_memory("no_such_task")
        assert result == []

    async def test_returns_rows_ordered_by_activation(self, stats_store: StatsStore) -> None:
        # Insert nodes with different activation counts
        await stats_store.upsert_working_memory(task_id="t1", node_id="n1", receipt_id="r1")
        await stats_store.upsert_working_memory(task_id="t1", node_id="n2", receipt_id="r2")
        await stats_store.upsert_working_memory(task_id="t1", node_id="n2", receipt_id="r3")
        await stats_store.upsert_working_memory(task_id="t1", node_id="n3", receipt_id="r4")
        await stats_store.upsert_working_memory(task_id="t1", node_id="n3", receipt_id="r5")
        await stats_store.upsert_working_memory(task_id="t1", node_id="n3", receipt_id="r6")

        rows = await stats_store.get_working_memory("t1")
        assert len(rows) == 3
        assert rows[0]["node_id"] == "n3"
        assert rows[0]["activation_count"] == 3
        assert rows[1]["node_id"] == "n2"
        assert rows[1]["activation_count"] == 2
        assert rows[2]["node_id"] == "n1"
        assert rows[2]["activation_count"] == 1

    async def test_only_returns_rows_for_given_task(self, stats_store: StatsStore) -> None:
        await stats_store.upsert_working_memory(task_id="t1", node_id="n1", receipt_id="r1")
        await stats_store.upsert_working_memory(task_id="t2", node_id="n2", receipt_id="r2")

        rows = await stats_store.get_working_memory("t1")
        assert len(rows) == 1
        assert rows[0]["node_id"] == "n1"


class TestEvictWorkingMemory:
    """evict_working_memory deletes by completed task IDs or TTL expiry."""

    async def test_evicts_by_completed_task_ids(self, stats_store: StatsStore) -> None:
        await stats_store.upsert_working_memory(task_id="t1", node_id="n1", receipt_id="r1")
        await stats_store.upsert_working_memory(task_id="t2", node_id="n2", receipt_id="r2")
        await stats_store.upsert_working_memory(task_id="t3", node_id="n3", receipt_id="r3")

        deleted = await stats_store.evict_working_memory(
            completed_task_ids=["t1", "t2"], ttl_days=365
        )
        assert deleted == 2

        # t3 should remain
        rows = await stats_store.get_working_memory("t3")
        assert len(rows) == 1
        assert await stats_store.get_working_memory("t1") == []
        assert await stats_store.get_working_memory("t2") == []

    async def test_evicts_by_ttl(self, stats_store: StatsStore) -> None:
        import aiosqlite

        # Insert a row with an old last_seen_at
        old_ts = "2020-01-01T00:00:00+00:00"
        async with aiosqlite.connect(stats_store.db_path) as db:
            await db.execute(
                "INSERT INTO working_memory "
                "(task_id, node_id, activation_count, first_seen_at, last_seen_at, last_receipt_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("old_task", "n1", 1, old_ts, old_ts, "r1"),
            )
            await db.commit()

        # Insert a fresh row
        await stats_store.upsert_working_memory(task_id="fresh_task", node_id="n2", receipt_id="r2")

        deleted = await stats_store.evict_working_memory(completed_task_ids=[], ttl_days=30)
        assert deleted == 1  # only old row

        assert await stats_store.get_working_memory("old_task") == []
        rows = await stats_store.get_working_memory("fresh_task")
        assert len(rows) == 1

    async def test_no_rows_to_evict(self, stats_store: StatsStore) -> None:
        deleted = await stats_store.evict_working_memory(completed_task_ids=[], ttl_days=30)
        assert deleted == 0

    async def test_empty_task_ids_with_no_expired(self, stats_store: StatsStore) -> None:
        await stats_store.upsert_working_memory(task_id="t1", node_id="n1", receipt_id="r1")
        deleted = await stats_store.evict_working_memory(completed_task_ids=[], ttl_days=365)
        assert deleted == 0
        rows = await stats_store.get_working_memory("t1")
        assert len(rows) == 1


class TestEnqueue:
    """enqueue validates exactly-one-of node_id / task_id."""

    async def test_enqueue_with_node_id(self, stats_store: StatsStore) -> None:
        await stats_store.enqueue("note.created", node_id="n1")
        result = await stats_store.drain_pending_nodes()
        assert len(result) == 1
        assert result[0]["node_id"] == "n1"

    async def test_enqueue_with_task_id(self, stats_store: StatsStore) -> None:
        await stats_store.enqueue("task.completed", task_id="t1")
        result = await stats_store.drain_pending_tasks()
        assert len(result) == 1
        assert result[0]["task_id"] == "t1"

    async def test_raises_when_both_none(self, stats_store: StatsStore) -> None:
        with pytest.raises(ValueError, match="Exactly one"):
            await stats_store.enqueue("note.created", node_id=None, task_id=None)

    async def test_raises_when_both_provided(self, stats_store: StatsStore) -> None:
        with pytest.raises(ValueError, match="Exactly one"):
            await stats_store.enqueue("note.created", node_id="x", task_id="y")


class TestDrainPendingNodes:
    """drain_pending_nodes: atomic claim, deduplication, trigger preservation."""

    async def test_returns_empty_when_no_pending(self, stats_store: StatsStore) -> None:
        result = await stats_store.drain_pending_nodes()
        assert result == []

    async def test_deduplicates_by_node_id(self, stats_store: StatsStore) -> None:
        await stats_store.enqueue("note.created", node_id="n1")
        await stats_store.enqueue("note.updated", node_id="n1")
        await stats_store.enqueue("retrieval", node_id="n1")

        result = await stats_store.drain_pending_nodes()
        assert len(result) == 1
        assert result[0]["node_id"] == "n1"
        assert set(result[0]["trigger_types"]) == {"note.created", "note.updated", "retrieval"}
        assert len(result[0]["claimed_ids"]) == 3

    async def test_rows_enqueued_after_drain_not_in_claimed_set(
        self, stats_store: StatsStore
    ) -> None:
        await stats_store.enqueue("note.created", node_id="n1")
        result1 = await stats_store.drain_pending_nodes()
        assert len(result1) == 1

        # Enqueue after drain
        await stats_store.enqueue("note.updated", node_id="n1")
        result2 = await stats_store.drain_pending_nodes()
        assert len(result2) == 1
        assert result2[0]["trigger_types"] == ["note.updated"]

    async def test_does_not_include_task_rows(self, stats_store: StatsStore) -> None:
        await stats_store.enqueue("task.completed", task_id="t1")
        result = await stats_store.drain_pending_nodes()
        assert result == []

    async def test_multiple_nodes(self, stats_store: StatsStore) -> None:
        await stats_store.enqueue("note.created", node_id="n1")
        await stats_store.enqueue("note.updated", node_id="n2")

        result = await stats_store.drain_pending_nodes()
        assert len(result) == 2
        node_ids = {r["node_id"] for r in result}
        assert node_ids == {"n1", "n2"}


class TestDrainPendingTasks:
    """drain_pending_tasks: atomic claim for task-level rows."""

    async def test_returns_empty_when_no_pending(self, stats_store: StatsStore) -> None:
        result = await stats_store.drain_pending_tasks()
        assert result == []

    async def test_claims_task_rows(self, stats_store: StatsStore) -> None:
        await stats_store.enqueue("task.completed", task_id="t1")
        await stats_store.enqueue("task.completed", task_id="t2")

        result = await stats_store.drain_pending_tasks()
        assert len(result) == 2
        task_ids = {r["task_id"] for r in result}
        assert task_ids == {"t1", "t2"}

    async def test_does_not_include_node_rows(self, stats_store: StatsStore) -> None:
        await stats_store.enqueue("note.created", node_id="n1")
        result = await stats_store.drain_pending_tasks()
        assert result == []

    async def test_rows_enqueued_after_drain_not_claimed(self, stats_store: StatsStore) -> None:
        await stats_store.enqueue("task.completed", task_id="t1")
        result1 = await stats_store.drain_pending_tasks()
        assert len(result1) == 1

        await stats_store.enqueue("task.completed", task_id="t2")
        result2 = await stats_store.drain_pending_tasks()
        assert len(result2) == 1
        assert result2[0]["task_id"] == "t2"


class TestRequeueFailed:
    """requeue_failed resets processed_at so rows reappear in next drain."""

    async def test_requeue_makes_rows_reappear(self, stats_store: StatsStore) -> None:
        await stats_store.enqueue("note.created", node_id="n1")
        result = await stats_store.drain_pending_nodes()
        claimed_ids = result[0]["claimed_ids"]

        # After drain, nothing pending
        assert await stats_store.drain_pending_nodes() == []

        # Requeue
        count = await stats_store.requeue_failed(claimed_ids)
        assert count == len(claimed_ids)

        # Now they reappear
        result2 = await stats_store.drain_pending_nodes()
        assert len(result2) == 1
        assert result2[0]["node_id"] == "n1"

    async def test_requeue_empty_list(self, stats_store: StatsStore) -> None:
        count = await stats_store.requeue_failed([])
        assert count == 0

    async def test_requeue_nonexistent_ids(self, stats_store: StatsStore) -> None:
        count = await stats_store.requeue_failed([99999])
        assert count == 0


class TestDrainConcurrency:
    """Concurrent drains must not claim the same row twice."""

    async def test_concurrent_drain_nodes_no_double_claim(self, stats_store: StatsStore) -> None:
        # Enqueue several rows
        for i in range(10):
            await stats_store.enqueue("note.created", node_id=f"n{i}")

        # Run two drains concurrently
        results = await asyncio.gather(
            stats_store.drain_pending_nodes(),
            stats_store.drain_pending_nodes(),
        )

        # Collect all claimed IDs across both results
        all_claimed: list[int] = []
        for result in results:
            for entry in result:
                assert isinstance(entry["claimed_ids"], list)
                all_claimed.extend(entry["claimed_ids"])

        # Each row must be claimed at most once
        assert len(all_claimed) == len(set(all_claimed)), "Some rows were double-claimed"
        # Together they should claim all 10 rows
        assert len(all_claimed) == 10

    async def test_concurrent_drain_tasks_no_double_claim(self, stats_store: StatsStore) -> None:
        for i in range(10):
            await stats_store.enqueue("task.completed", task_id=f"t{i}")

        results = await asyncio.gather(
            stats_store.drain_pending_tasks(),
            stats_store.drain_pending_tasks(),
        )

        all_claimed: list[int] = []
        for result in results:
            for entry in result:
                assert isinstance(entry["claimed_ids"], list)
                all_claimed.extend(entry["claimed_ids"])

        assert len(all_claimed) == len(set(all_claimed)), "Some rows were double-claimed"
        assert len(all_claimed) == 10


class TestGetReceipt:
    """get_receipt: look up receipt by ID with task_id verification."""

    async def test_returns_correct_receipt_and_final_node_ids(
        self, stats_store: StatsStore
    ) -> None:
        final_nodes = [{"id": "node-a", "score": 0.9}, {"id": "node-b", "score": 0.7}]
        await stats_store.insert_receipt(
            receipt_id="rcpt_test1",
            query="test query",
            limit=10,
            namespace_filter=None,
            scouts_fired=["scout_vector"],
            candidates_considered=5,
            final_nodes=final_nodes,
            conflicts_surfaced=[],
            surface_conflicts=False,
            temperature=0.5,
            terrace_reached=1,
            agent_id="agent-1",
            task_id="task-1",
        )

        result = await stats_store.get_receipt("rcpt_test1", "task-1")
        assert result is not None
        assert result["id"] == "rcpt_test1"
        assert result["task_id"] == "task-1"
        assert result["final_node_ids"] == ["node-a", "node-b"]

    async def test_mismatched_task_id_returns_none(self, stats_store: StatsStore) -> None:
        await stats_store.insert_receipt(
            receipt_id="rcpt_test2",
            query="test query",
            limit=10,
            namespace_filter=None,
            scouts_fired=["scout_vector"],
            candidates_considered=5,
            final_nodes=[{"id": "node-x"}],
            conflicts_surfaced=[],
            surface_conflicts=False,
            temperature=0.5,
            terrace_reached=1,
            agent_id="agent-1",
            task_id="task-1",
        )

        result = await stats_store.get_receipt("rcpt_test2", "wrong-task")
        assert result is None

    async def test_nonexistent_receipt_returns_none(self, stats_store: StatsStore) -> None:
        result = await stats_store.get_receipt("rcpt_nonexistent", "task-1")
        assert result is None


class TestGetLatestReceipt:
    """get_latest_receipt: return most recent receipt for (task_id, agent_id)."""

    async def test_returns_most_recent_receipt(self, stats_store: StatsStore) -> None:
        for i in range(3):
            await stats_store.insert_receipt(
                receipt_id=f"rcpt_latest_{i}",
                query=f"query {i}",
                limit=10,
                namespace_filter=None,
                scouts_fired=["scout_vector"],
                candidates_considered=5,
                final_nodes=[{"id": f"node-{i}"}],
                conflicts_surfaced=[],
                surface_conflicts=False,
                temperature=0.5,
                terrace_reached=1,
                agent_id="agent-1",
                task_id="task-1",
            )

        result = await stats_store.get_latest_receipt("task-1", "agent-1")
        assert result is not None
        assert result["id"] == "rcpt_latest_2"
        assert result["final_node_ids"] == ["node-2"]

    async def test_no_matching_receipt_returns_none(self, stats_store: StatsStore) -> None:
        result = await stats_store.get_latest_receipt("no-task", "no-agent")
        assert result is None


class TestGetCoactivated:
    """get_coactivated: return co-occurring nodes ranked by count."""

    async def test_returns_correct_pairs_descending(self, stats_store: StatsStore) -> None:
        # Seed coactivation data: "a" co-occurs with "b" (3x) and "c" (1x)
        for _ in range(3):
            await stats_store.increment_coactivation(node_a="a", node_b="b", namespace="ns1")
        await stats_store.increment_coactivation(node_a="a", node_b="c", namespace="ns1")

        result = await stats_store.get_coactivated(["a"])
        assert len(result) == 2
        assert result[0] == ("b", 3)
        assert result[1] == ("c", 1)

    async def test_namespace_filter(self, stats_store: StatsStore) -> None:
        await stats_store.increment_coactivation(node_a="a", node_b="b", namespace="ns1")
        await stats_store.increment_coactivation(node_a="a", node_b="c", namespace="ns2")

        result = await stats_store.get_coactivated(["a"], namespace="ns1")
        assert len(result) == 1
        assert result[0] == ("b", 1)

    async def test_deduplicates_across_seeds(self, stats_store: StatsStore) -> None:
        # Both seeds co-occur with "x"
        await stats_store.increment_coactivation(node_a="a", node_b="x", namespace="ns1")
        await stats_store.increment_coactivation(node_a="b", node_b="x", namespace="ns1")

        result = await stats_store.get_coactivated(["a", "b"])
        # "x" appears via both seeds, counts should be summed
        assert len(result) == 1
        assert result[0] == ("x", 2)

    async def test_excludes_seed_nodes(self, stats_store: StatsStore) -> None:
        # "a" and "b" co-occur; when both are seeds, neither should appear in results
        await stats_store.increment_coactivation(node_a="a", node_b="b", namespace="ns1")
        await stats_store.increment_coactivation(node_a="a", node_b="c", namespace="ns1")

        result = await stats_store.get_coactivated(["a", "b"])
        assert len(result) == 1
        assert result[0] == ("c", 1)

    async def test_respects_limit(self, stats_store: StatsStore) -> None:
        for i in range(5):
            for _ in range(5 - i):
                await stats_store.increment_coactivation(
                    node_a="seed", node_b=f"n{i}", namespace="ns1"
                )

        result = await stats_store.get_coactivated(["seed"], limit=2)
        assert len(result) == 2
        assert result[0] == ("n0", 5)
        assert result[1] == ("n1", 4)

    async def test_empty_node_ids(self, stats_store: StatsStore) -> None:
        result = await stats_store.get_coactivated([])
        assert result == []

    async def test_finds_via_both_columns(self, stats_store: StatsStore) -> None:
        """Seed may appear as node_id_a or node_id_b depending on lex order."""
        # "z" > "a", so stored as (a, z) — seed "z" is in node_id_b column
        await stats_store.increment_coactivation(node_a="z", node_b="a", namespace="ns1")
        # "z" < "zz", so stored as (z, zz) — seed "z" is in node_id_a column
        await stats_store.increment_coactivation(node_a="z", node_b="zz", namespace="ns1")

        result = await stats_store.get_coactivated(["z"])
        ids = {r[0] for r in result}
        assert ids == {"a", "zz"}


class TestConsolidationTracking:
    """Per-target op tables correctly track individual edge and salience operations."""

    async def test_task_consolidation_log(self, stats_store: StatsStore) -> None:
        assert not await stats_store.is_task_consolidated("task-1")
        await stats_store.mark_task_consolidated("task-1")
        assert await stats_store.is_task_consolidated("task-1")
        # Idempotent — second call is INSERT OR IGNORE
        await stats_store.mark_task_consolidated("task-1")
        assert await stats_store.is_task_consolidated("task-1")

    async def test_consolidation_edge_ops(self, stats_store: StatsStore) -> None:
        assert not await stats_store.has_consolidation_edge_op("t1", "a", "b")
        await stats_store.record_consolidation_edge_op("t1", "a", "b")
        assert await stats_store.has_consolidation_edge_op("t1", "a", "b")
        # Different task — not recorded
        assert not await stats_store.has_consolidation_edge_op("t2", "a", "b")
        # Different pair — not recorded
        assert not await stats_store.has_consolidation_edge_op("t1", "a", "c")
        # Idempotent
        await stats_store.record_consolidation_edge_op("t1", "a", "b")
        assert await stats_store.has_consolidation_edge_op("t1", "a", "b")

    async def test_consolidation_salience_ops(self, stats_store: StatsStore) -> None:
        assert not await stats_store.has_consolidation_salience_op("t1", "node-1")
        await stats_store.record_consolidation_salience_op("t1", "node-1")
        assert await stats_store.has_consolidation_salience_op("t1", "node-1")
        # Different task — not recorded
        assert not await stats_store.has_consolidation_salience_op("t2", "node-1")
        # Different node — not recorded
        assert not await stats_store.has_consolidation_salience_op("t1", "node-2")
        # Idempotent
        await stats_store.record_consolidation_salience_op("t1", "node-1")
        assert await stats_store.has_consolidation_salience_op("t1", "node-1")
