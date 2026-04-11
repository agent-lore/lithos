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
    retrieval_count INTEGER NOT NULL DEFAULT 0,
    last_retrieved_at TIMESTAMP,
    salience REAL NOT NULL DEFAULT 0.5
);

CREATE TABLE IF NOT EXISTS coactivation (
    node_a TEXT NOT NULL,
    node_b TEXT NOT NULL,
    namespace TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    last_at TIMESTAMP,
    PRIMARY KEY (node_a, node_b, namespace)
);

CREATE TABLE IF NOT EXISTS enrich_queue (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    enrich_type TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS working_memory (
    task_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    activation_count INTEGER NOT NULL DEFAULT 0,
    last_seen_at TIMESTAMP,
    last_receipt_id TEXT,
    PRIMARY KEY (task_id, node_id)
);

CREATE TABLE IF NOT EXISTS receipts (
    id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    "limit" INTEGER NOT NULL,
    namespace_filter TEXT,
    scouts_fired TEXT NOT NULL,
    final_nodes TEXT NOT NULL,
    conflicts_surfaced TEXT NOT NULL,
    temperature REAL NOT NULL,
    terrace_reached INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    agent_id TEXT,
    task_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_receipts_created_at ON receipts(created_at);
CREATE INDEX IF NOT EXISTS idx_working_memory_task_id ON working_memory(task_id);
CREATE INDEX IF NOT EXISTS idx_enrich_queue_status ON enrich_queue(status);
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
        final_nodes: list[str],
        conflicts_surfaced: list[str],
        temperature: float,
        terrace_reached: int,
        agent_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        """Insert a single receipt row."""
        await self._ensure_open()
        ns_json = json.dumps(namespace_filter) if namespace_filter is not None else None
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO receipts
                   (id, query, "limit", namespace_filter, scouts_fired,
                    final_nodes, conflicts_surfaced, temperature,
                    terrace_reached, agent_id, task_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    receipt_id,
                    query,
                    limit,
                    ns_json,
                    json.dumps(scouts_fired),
                    json.dumps(final_nodes),
                    json.dumps(conflicts_surfaced),
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
        """Upsert a working-memory row, incrementing activation_count."""
        await self._ensure_open()
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO working_memory
                   (task_id, node_id, activation_count, last_seen_at, last_receipt_id)
                   VALUES (?, ?, 1, ?, ?)
                   ON CONFLICT(task_id, node_id) DO UPDATE SET
                     activation_count = activation_count + 1,
                     last_seen_at = excluded.last_seen_at,
                     last_receipt_id = excluded.last_receipt_id""",
                (task_id, node_id, now, receipt_id),
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
                """INSERT INTO coactivation (node_a, node_b, namespace, count, last_at)
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(node_a, node_b, namespace) DO UPDATE SET
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
