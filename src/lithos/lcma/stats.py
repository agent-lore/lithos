"""LCMA stats store — lazily-created SQLite database for retrieval stats.

Follows the coordination.db / edges.db pattern: async via aiosqlite,
single-writer safe, corrupt-DB quarantine with automatic recreation.

Tables (MVP 1):
  node_stats      — per-node retrieval counts and salience
  coactivation    — pairwise co-occurrence counts from result sets
  enrich_queue    — queue for deferred enrichment jobs
  working_memory  — per-task node activation tracking
  receipts        — audit trail for every lithos_retrieve call
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from lithos.config import LithosConfig, get_config

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS node_stats (
    node_id TEXT PRIMARY KEY,
    salience REAL NOT NULL DEFAULT 0.5,
    retrieval_count INTEGER NOT NULL DEFAULT 0,
    last_retrieved_at TIMESTAMP,
    last_used_at TIMESTAMP,
    ignored_count INTEGER NOT NULL DEFAULT 0,
    misleading_count INTEGER NOT NULL DEFAULT 0,
    decay_rate REAL NOT NULL DEFAULT 0.0,
    spaced_rep_strength REAL NOT NULL DEFAULT 0.0,
    cited_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS coactivation (
    node_id_a TEXT NOT NULL,
    node_id_b TEXT NOT NULL,
    namespace TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    last_at TIMESTAMP,
    PRIMARY KEY (node_id_a, node_id_b, namespace)
);

CREATE TABLE IF NOT EXISTS enrich_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_type TEXT NOT NULL,
    node_id TEXT,
    task_id TEXT,
    triggered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS working_memory (
    task_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    activation_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at TIMESTAMP,
    last_seen_at TIMESTAMP,
    last_receipt_id TEXT,
    PRIMARY KEY (task_id, node_id)
);

CREATE TABLE IF NOT EXISTS receipts (
    id TEXT PRIMARY KEY,
    ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    query TEXT NOT NULL,
    "limit" INTEGER NOT NULL,
    namespace_filter TEXT,
    scouts_fired TEXT NOT NULL,
    candidates_considered INTEGER NOT NULL DEFAULT 0,
    final_nodes TEXT NOT NULL,
    conflicts_surfaced TEXT NOT NULL,
    surface_conflicts INTEGER NOT NULL DEFAULT 0,
    temperature REAL NOT NULL,
    terrace_reached INTEGER NOT NULL DEFAULT 0,
    agent_id TEXT,
    task_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_receipts_ts ON receipts(ts);
CREATE INDEX IF NOT EXISTS idx_receipts_task_id ON receipts(task_id);
CREATE INDEX IF NOT EXISTS idx_receipts_agent_id ON receipts(agent_id);
CREATE INDEX IF NOT EXISTS idx_working_memory_task_id ON working_memory(task_id);
CREATE INDEX IF NOT EXISTS idx_enrich_queue_processed_at ON enrich_queue(processed_at);
CREATE INDEX IF NOT EXISTS idx_coactivation_namespace ON coactivation(namespace);
"""


def _generate_receipt_id() -> str:
    """Generate a receipt ID in the form ``rcpt_<short-uuid>``."""
    return f"rcpt_{uuid.uuid4().hex[:12]}"


class StatsStore:
    """Lazily-created SQLite store for LCMA retrieval statistics.

    The database file is created on the first call to :meth:`open`.
    Corrupt databases are quarantined (renamed) and recreated with an
    empty schema.
    """

    def __init__(self, config: LithosConfig | None = None) -> None:
        self._config = config
        self._opened = False

    @property
    def config(self) -> LithosConfig:
        return self._config or get_config()

    @property
    def db_path(self) -> Path:
        return self.config.storage.stats_db_path

    async def open(self) -> None:
        """Ensure stats.db exists with the correct schema.

        Idempotent — safe to call multiple times.  If the file is corrupt
        it is quarantined and a fresh database is created.
        """
        if self._opened:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        if self.db_path.exists():
            healthy = await self._probe(self.db_path)
            if not healthy:
                self._quarantine(self.db_path)

        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await self._migrate_add_cited_count(db)
            await db.commit()
        self._opened = True

    async def _ensure_open(self) -> None:
        """Lazily create the database on first use."""
        if not self._opened:
            await self.open()

    # ------------------------------------------------------------------
    # Receipt operations
    # ------------------------------------------------------------------

    async def insert_receipt(
        self,
        *,
        receipt_id: str,
        query: str,
        limit: int,
        namespace_filter: list[str] | None,
        scouts_fired: list[str],
        candidates_considered: int,
        final_nodes: list[dict[str, object]],
        conflicts_surfaced: list[dict[str, object]],
        surface_conflicts: bool,
        temperature: float,
        terrace_reached: int,
        agent_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        """Insert a single receipt row.

        ``final_nodes`` is a JSON-serialisable list of objects, each with
        at least an ``id`` field plus any explainability metadata
        (typically ``reasons`` and ``scouts``). The shape matches design
        §4.6 so future ``lithos_receipts`` queries can render audit trails
        without re-walking the retrieval pipeline.
        """
        await self._ensure_open()
        ns_json = json.dumps(namespace_filter) if namespace_filter is not None else None
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO receipts
                   (id, query, "limit", namespace_filter, scouts_fired,
                    candidates_considered, final_nodes, conflicts_surfaced,
                    surface_conflicts, temperature, terrace_reached,
                    agent_id, task_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    receipt_id,
                    query,
                    limit,
                    ns_json,
                    json.dumps(scouts_fired),
                    candidates_considered,
                    json.dumps(final_nodes),
                    json.dumps(conflicts_surfaced),
                    1 if surface_conflicts else 0,
                    temperature,
                    terrace_reached,
                    agent_id,
                    task_id,
                ),
            )
            await db.commit()

    # ------------------------------------------------------------------
    # Working-memory operations
    # ------------------------------------------------------------------

    async def upsert_working_memory(
        self,
        *,
        task_id: str,
        node_id: str,
        receipt_id: str,
    ) -> None:
        """Upsert a working-memory row, incrementing activation_count.

        ``first_seen_at`` is set on INSERT only — existing rows preserve
        their original value so callers can distinguish first activation
        from subsequent touches.
        """
        await self._ensure_open()
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO working_memory
                   (task_id, node_id, activation_count,
                    first_seen_at, last_seen_at, last_receipt_id)
                   VALUES (?, ?, 1, ?, ?, ?)
                   ON CONFLICT(task_id, node_id) DO UPDATE SET
                     activation_count = activation_count + 1,
                     last_seen_at = excluded.last_seen_at,
                     last_receipt_id = excluded.last_receipt_id""",
                (task_id, node_id, now, now, receipt_id),
            )
            await db.commit()

    # ------------------------------------------------------------------
    # Coactivation operations
    # ------------------------------------------------------------------

    async def increment_coactivation(
        self,
        *,
        node_a: str,
        node_b: str,
        namespace: str,
    ) -> None:
        """Increment coactivation count for an unordered pair."""
        await self._ensure_open()
        a, b = (node_a, node_b) if node_a <= node_b else (node_b, node_a)
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO coactivation
                   (node_id_a, node_id_b, namespace, count, last_at)
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(node_id_a, node_id_b, namespace) DO UPDATE SET
                     count = count + 1,
                     last_at = excluded.last_at""",
                (a, b, namespace, now),
            )
            await db.commit()

    # ------------------------------------------------------------------
    # Node stats operations
    # ------------------------------------------------------------------

    async def increment_node_stats(self, *, node_id: str) -> None:
        """Increment retrieval_count and update last_retrieved_at for a node.

        Inserts with salience=0.5 on first touch.
        """
        await self._ensure_open()
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO node_stats (node_id, retrieval_count, last_retrieved_at, salience)
                   VALUES (?, 1, ?, 0.5)
                   ON CONFLICT(node_id) DO UPDATE SET
                     retrieval_count = retrieval_count + 1,
                     last_retrieved_at = excluded.last_retrieved_at""",
                (node_id, now),
            )
            await db.commit()

    async def get_node_stats(self, node_id: str) -> dict[str, object] | None:
        """Return all node_stats columns for *node_id*, or ``None`` if absent."""
        await self._ensure_open()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM node_stats WHERE node_id = ?", (node_id,))
            row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    async def update_salience(self, node_id: str, delta: float) -> None:
        """Atomically adjust salience by *delta*, clamping to [0.0, 1.0].

        Creates the row with ``salience = 0.5 + delta`` (clamped) if absent.
        """
        await self._ensure_open()
        initial = max(0.0, min(1.0, 0.5 + delta))
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO node_stats (node_id, salience)
                   VALUES (?, ?)
                   ON CONFLICT(node_id) DO UPDATE SET
                     salience = MAX(0.0, MIN(1.0, salience + ?))""",
                (node_id, initial, delta),
            )
            await db.commit()

    async def increment_ignored(self, node_id: str) -> None:
        """Atomically increment ignored_count; creates row if absent."""
        await self._ensure_open()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO node_stats (node_id, ignored_count)
                   VALUES (?, 1)
                   ON CONFLICT(node_id) DO UPDATE SET
                     ignored_count = ignored_count + 1""",
                (node_id,),
            )
            await db.commit()

    async def increment_cited(self, node_id: str) -> None:
        """Atomically increment cited_count; creates row if absent."""
        await self._ensure_open()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO node_stats (node_id, cited_count)
                   VALUES (?, 1)
                   ON CONFLICT(node_id) DO UPDATE SET
                     cited_count = cited_count + 1""",
                (node_id,),
            )
            await db.commit()

    async def increment_misleading(self, node_id: str) -> None:
        """Atomically increment misleading_count; creates row if absent."""
        await self._ensure_open()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO node_stats (node_id, misleading_count)
                   VALUES (?, 1)
                   ON CONFLICT(node_id) DO UPDATE SET
                     misleading_count = misleading_count + 1""",
                (node_id,),
            )
            await db.commit()

    async def update_spaced_rep_strength(self, node_id: str, delta: float) -> None:
        """Atomically adjust spaced_rep_strength by *delta*, clamping to [0.0, 1.0].

        Creates the row with ``spaced_rep_strength = max(0, min(1, 0 + delta))``
        if absent (default is 0.0).
        """
        await self._ensure_open()
        initial = max(0.0, min(1.0, delta))
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO node_stats (node_id, spaced_rep_strength)
                   VALUES (?, ?)
                   ON CONFLICT(node_id) DO UPDATE SET
                     spaced_rep_strength = MAX(0.0, MIN(1.0, spaced_rep_strength + ?))""",
                (node_id, initial, delta),
            )
            await db.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _migrate_add_cited_count(db: aiosqlite.Connection) -> None:
        """Add cited_count column to existing node_stats tables."""
        cursor = await db.execute("PRAGMA table_info(node_stats)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "cited_count" not in columns:
            await db.execute(
                "ALTER TABLE node_stats ADD COLUMN cited_count INTEGER NOT NULL DEFAULT 0"
            )

    @staticmethod
    async def _probe(path: Path) -> bool:
        """Return True if *path* is a usable SQLite database."""
        try:
            async with aiosqlite.connect(path) as db:
                await db.execute("PRAGMA integrity_check")
            return True
        except Exception:
            return False

    @staticmethod
    def _quarantine(path: Path) -> Path:
        """Rename a corrupt database file and return the backup path."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.corrupt-{timestamp}")
        suffix = 1
        while backup.exists():
            backup = path.with_name(f"{path.name}.corrupt-{timestamp}-{suffix}")
            suffix += 1
        path.rename(backup)
        logger.warning("Quarantined corrupt stats.db → %s", backup)
        return backup
