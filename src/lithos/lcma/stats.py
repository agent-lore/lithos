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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from lithos.config import LithosConfig, get_config

if TYPE_CHECKING:
    from lithos.telemetry import _LithosMetrics

_lithos_metrics: _LithosMetrics | None = None
try:
    from lithos.telemetry import lithos_metrics as _lithos_metrics

    _HAS_TELEMETRY = True
except Exception:
    _HAS_TELEMETRY = False

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
    processed_at TIMESTAMP,
    attempts INTEGER NOT NULL DEFAULT 0
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

CREATE TABLE IF NOT EXISTS task_consolidation_log (
    task_id TEXT PRIMARY KEY,
    consolidated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS consolidation_edge_ops (
    task_id TEXT NOT NULL,
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (task_id, from_id, to_id)
);

CREATE TABLE IF NOT EXISTS consolidation_salience_ops (
    task_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (task_id, node_id)
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


def _extract_final_node_ids(final_nodes_json: object) -> list[str]:
    """Parse the ``final_nodes`` JSON column and collect ``id`` fields."""
    if not isinstance(final_nodes_json, str):
        return []
    try:
        entries = json.loads(final_nodes_json)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(entries, list):
        return []
    return [entry["id"] for entry in entries if isinstance(entry, dict) and "id" in entry]


class StatsStore:
    """Lazily-created SQLite store for LCMA retrieval statistics.

    The database file is created on the first call to :meth:`open`.
    Corrupt databases are quarantined (renamed) and recreated with an
    empty schema.
    """

    def __init__(self, config: LithosConfig | None = None) -> None:
        self._config = config
        self._opened = False
        self._cached_enrich_queue_depth: int = 0
        self._cached_coactivation_pairs: int = 0
        self._cached_working_memory_active_tasks: int = 0

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
            await self._migrate_add_last_decay_applied_at(db)
            await self._migrate_add_enrich_queue_attempts(db)
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

    async def get_working_memory(self, task_id: str) -> list[dict[str, object]]:
        """Return all working_memory rows for *task_id*, ordered by activation_count descending."""
        await self._ensure_open()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM working_memory WHERE task_id = ? ORDER BY activation_count DESC",
                (task_id,),
            )
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def evict_working_memory(self, *, completed_task_ids: list[str], ttl_days: int) -> int:
        """Delete working_memory rows for completed tasks or stale entries.

        Removes rows where ``task_id`` is in *completed_task_ids* **OR**
        ``last_seen_at`` is older than *ttl_days* ago.  Returns the count
        of rows deleted.
        """
        await self._ensure_open()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            if completed_task_ids:
                placeholders = ", ".join("?" for _ in completed_task_ids)
                cursor = await db.execute(
                    f"DELETE FROM working_memory "
                    f"WHERE task_id IN ({placeholders}) OR last_seen_at < ?",
                    (*completed_task_ids, cutoff),
                )
            else:
                cursor = await db.execute(
                    "DELETE FROM working_memory WHERE last_seen_at < ?",
                    (cutoff,),
                )
            deleted = cursor.rowcount
            await db.commit()
        return deleted

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

    async def get_coactivated(
        self,
        node_ids: list[str],
        *,
        namespace: str | None = None,
        limit: int = 20,
    ) -> list[tuple[str, int]]:
        """Return nodes frequently co-occurring with *node_ids* in past retrievals.

        Queries the coactivation table for rows where any of *node_ids*
        appears as either ``node_id_a`` or ``node_id_b``.  For each match
        the *other* node in the pair is collected with its ``count``.

        When the same other-node appears across multiple seeds, counts are
        summed.  Seed nodes themselves are excluded from results.

        Returns ``(other_node_id, total_count)`` pairs sorted by count
        descending, capped at *limit*.
        """
        if not node_ids:
            return []
        await self._ensure_open()
        seed_set = set(node_ids)
        totals: dict[str, int] = {}

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            placeholders = ", ".join("?" for _ in node_ids)

            # Rows where a seed appears as node_id_a → other is node_id_b
            if namespace is not None:
                query_a = (
                    f"SELECT node_id_b AS other, count FROM coactivation "
                    f"WHERE node_id_a IN ({placeholders}) AND namespace = ?"
                )
                params_a: tuple[str, ...] = (*node_ids, namespace)
                query_b = (
                    f"SELECT node_id_a AS other, count FROM coactivation "
                    f"WHERE node_id_b IN ({placeholders}) AND namespace = ?"
                )
                params_b: tuple[str, ...] = (*node_ids, namespace)
            else:
                query_a = (
                    f"SELECT node_id_b AS other, count FROM coactivation "
                    f"WHERE node_id_a IN ({placeholders})"
                )
                params_a = tuple(node_ids)
                query_b = (
                    f"SELECT node_id_a AS other, count FROM coactivation "
                    f"WHERE node_id_b IN ({placeholders})"
                )
                params_b = tuple(node_ids)

            for query, params in [(query_a, params_a), (query_b, params_b)]:
                cursor = await db.execute(query, params)
                for row in await cursor.fetchall():
                    other = row["other"]
                    if other in seed_set:
                        continue
                    totals[other] = totals.get(other, 0) + row["count"]

        ranked = sorted(totals.items(), key=lambda t: t[1], reverse=True)
        return ranked[:limit]

    # ------------------------------------------------------------------
    # Enrich-queue operations
    # ------------------------------------------------------------------

    async def enqueue(
        self,
        trigger_type: str,
        *,
        node_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        """Insert a row into enrich_queue.

        Exactly one of *node_id* or *task_id* must be provided.
        """
        if (node_id is None) == (task_id is None):
            raise ValueError("Exactly one of node_id or task_id must be provided")
        await self._ensure_open()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO enrich_queue (trigger_type, node_id, task_id) VALUES (?, ?, ?)",
                (trigger_type, node_id, task_id),
            )
            await db.commit()

    async def drain_pending_nodes(
        self, *, max_attempts: int | None = None
    ) -> list[dict[str, object]]:
        """Atomically claim unprocessed node-level enrich_queue rows.

        Returns ``[{node_id, trigger_types, claimed_ids}]`` deduplicated
        by ``node_id``, preserving all distinct trigger types per node.
        ``note.created`` and ``note.updated`` triggers are never discarded.

        Args:
            max_attempts: When provided, only claim rows whose ``attempts``
                column is strictly less than this value.
        """
        await self._ensure_open()
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # BEGIN IMMEDIATE acquires a write lock before the SELECT,
            # preventing concurrent callers from reading the same rows.
            await db.execute("BEGIN IMMEDIATE")
            if max_attempts is not None:
                cursor = await db.execute(
                    "SELECT id, node_id, trigger_type FROM enrich_queue "
                    "WHERE node_id IS NOT NULL AND processed_at IS NULL "
                    "AND attempts < ?",
                    (max_attempts,),
                )
            else:
                cursor = await db.execute(
                    "SELECT id, node_id, trigger_type FROM enrich_queue "
                    "WHERE node_id IS NOT NULL AND processed_at IS NULL"
                )
            rows = await cursor.fetchall()
            if not rows:
                await db.execute("COMMIT")
                return []

            # Mark as processed within the same transaction
            ids = [row["id"] for row in rows]
            placeholders = ", ".join("?" for _ in ids)
            await db.execute(
                f"UPDATE enrich_queue SET processed_at = ? WHERE id IN ({placeholders})",
                (now, *ids),
            )
            await db.commit()

        # Deduplicate by node_id
        by_node: dict[str, dict[str, object]] = {}
        for row in rows:
            nid = row["node_id"]
            if nid not in by_node:
                by_node[nid] = {
                    "node_id": nid,
                    "trigger_types": set(),
                    "claimed_ids": [],
                }
            entry = by_node[nid]
            assert isinstance(entry["trigger_types"], set)
            assert isinstance(entry["claimed_ids"], list)
            entry["trigger_types"].add(row["trigger_type"])
            entry["claimed_ids"].append(row["id"])

        # Convert sets to sorted lists for deterministic output
        result: list[dict[str, object]] = []
        for entry in by_node.values():
            assert isinstance(entry["trigger_types"], set)
            entry["trigger_types"] = sorted(entry["trigger_types"])
            result.append(entry)
        return result

    async def drain_pending_tasks(
        self, *, max_attempts: int | None = None
    ) -> list[dict[str, object]]:
        """Atomically claim unprocessed task-level enrich_queue rows.

        Returns ``[{task_id, claimed_ids}]`` using the same atomic
        SELECT+UPDATE pattern as :meth:`drain_pending_nodes`.

        Args:
            max_attempts: When provided, only claim rows whose ``attempts``
                column is strictly less than this value.
        """
        await self._ensure_open()
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # BEGIN IMMEDIATE acquires a write lock before the SELECT,
            # preventing concurrent callers from reading the same rows.
            await db.execute("BEGIN IMMEDIATE")
            if max_attempts is not None:
                cursor = await db.execute(
                    "SELECT id, task_id FROM enrich_queue "
                    "WHERE node_id IS NULL AND processed_at IS NULL "
                    "AND attempts < ?",
                    (max_attempts,),
                )
            else:
                cursor = await db.execute(
                    "SELECT id, task_id FROM enrich_queue "
                    "WHERE node_id IS NULL AND processed_at IS NULL"
                )
            rows = await cursor.fetchall()
            if not rows:
                await db.execute("COMMIT")
                return []

            ids = [row["id"] for row in rows]
            placeholders = ", ".join("?" for _ in ids)
            await db.execute(
                f"UPDATE enrich_queue SET processed_at = ? WHERE id IN ({placeholders})",
                (now, *ids),
            )
            await db.commit()

        # Deduplicate by task_id
        by_task: dict[str, dict[str, object]] = {}
        for row in rows:
            tid = row["task_id"]
            if tid not in by_task:
                by_task[tid] = {"task_id": tid, "claimed_ids": []}
            entry = by_task[tid]
            assert isinstance(entry["claimed_ids"], list)
            entry["claimed_ids"].append(row["id"])

        return list(by_task.values())

    async def requeue_failed(self, claimed_ids: list[int]) -> int:
        """Reset ``processed_at`` to NULL and increment ``attempts`` for the given row IDs.

        Returns the count of rows reset.
        """
        if not claimed_ids:
            return 0
        await self._ensure_open()
        placeholders = ", ".join("?" for _ in claimed_ids)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE enrich_queue SET processed_at = NULL, attempts = attempts + 1 "
                f"WHERE id IN ({placeholders})",
                tuple(claimed_ids),
            )
            count = cursor.rowcount
            await db.commit()
        return count

    async def get_exhausted_items(
        self, claimed_ids: list[int], max_attempts: int
    ) -> list[dict[str, object]]:
        """Return enrich_queue rows among *claimed_ids* whose attempts >= *max_attempts*."""
        if not claimed_ids:
            return []
        await self._ensure_open()
        placeholders = ", ".join("?" for _ in claimed_ids)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT id, node_id, task_id, attempts FROM enrich_queue "
                f"WHERE id IN ({placeholders}) AND attempts >= ?",
                (*claimed_ids, max_attempts),
            )
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_enrich_items_by_ids(self, item_ids: list[int]) -> list[dict[str, object]]:
        """Return enrich_queue rows for the given IDs including triggered_at, processed_at, attempts.

        Used by the enrichment worker to record OTEL metrics after draining.
        """
        if not item_ids:
            return []
        await self._ensure_open()
        placeholders = ", ".join("?" for _ in item_ids)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT id, triggered_at, processed_at, attempts FROM enrich_queue "
                f"WHERE id IN ({placeholders})",
                tuple(item_ids),
            )
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

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

    async def increment_node_stats_batch(self, node_ids: list[str]) -> None:
        """Increment retrieval_count for multiple nodes in a single transaction."""
        if not node_ids:
            return
        await self._ensure_open()
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                """INSERT INTO node_stats (node_id, retrieval_count, last_retrieved_at, salience)
                   VALUES (?, 1, ?, 0.5)
                   ON CONFLICT(node_id) DO UPDATE SET
                     retrieval_count = retrieval_count + 1,
                     last_retrieved_at = excluded.last_retrieved_at""",
                [(nid, now) for nid in node_ids],
            )
            await db.commit()

    async def increment_coactivation_batch(
        self,
        pairs: list[tuple[str, str]],
        namespace: str,
    ) -> None:
        """Increment coactivation counts for multiple pairs in a single transaction."""
        if not pairs:
            return
        await self._ensure_open()
        now = datetime.now(timezone.utc).isoformat()
        # Canonicalize pairs so node_id_a <= node_id_b
        canonical = [(min(a, b), max(a, b)) for a, b in pairs]
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                """INSERT INTO coactivation (node_id_a, node_id_b, namespace, count, last_at)
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(node_id_a, node_id_b, namespace) DO UPDATE SET
                     count = count + 1,
                     last_at = excluded.last_at""",
                [(a, b, namespace, now) for a, b in canonical],
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

    async def get_node_stats_batch(self, node_ids: list[str]) -> dict[str, dict[str, object]]:
        """Return node_stats rows for multiple nodes in a single query.

        Returns a mapping of ``node_id -> dict`` for rows that exist.
        Missing nodes are omitted from the result.
        """
        if not node_ids:
            return {}
        await self._ensure_open()
        placeholders = ",".join("?" for _ in node_ids)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT * FROM node_stats WHERE node_id IN ({placeholders})",
                node_ids,
            )
            rows = await cursor.fetchall()
        return {row["node_id"]: dict(row) for row in rows}

    async def list_all_node_ids(self) -> list[str]:
        """Return all node_id values from node_stats."""
        await self._ensure_open()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT node_id FROM node_stats")
            rows = await cursor.fetchall()
        return [row[0] for row in rows]

    # ------------------------------------------------------------------
    # Observable gauge helpers (synchronous cached counts for OTEL)
    # ------------------------------------------------------------------

    # These are populated by _refresh_cached_counts() which is called
    # periodically by the OTEL metric collection loop via register_lcma_metrics.

    def get_cached_enrich_queue_depth(self) -> int:
        """Return the last cached enrich_queue unprocessed item count (sync, cheap)."""
        return self._cached_enrich_queue_depth

    def get_cached_coactivation_pairs(self) -> int:
        """Return the last cached coactivation row count (sync, cheap)."""
        return self._cached_coactivation_pairs

    def get_cached_working_memory_active_tasks(self) -> int:
        """Return the last cached active working_memory task count (sync, cheap)."""
        return self._cached_working_memory_active_tasks

    async def refresh_cached_counts(self) -> None:
        """Refresh the cached gauge values from the database.

        Called periodically (e.g. from the enrich drain loop) so that OTEL
        observable gauges always report a recent value without requiring async
        callbacks in the SDK metric collection path.
        """
        await self._ensure_open()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute("SELECT COUNT(*) FROM enrich_queue WHERE processed_at IS NULL")
            ).fetchone()
            self._cached_enrich_queue_depth = int(row[0]) if row else 0

            row = await (await db.execute("SELECT COUNT(*) FROM coactivation")).fetchone()
            self._cached_coactivation_pairs = int(row[0]) if row else 0

            row = await (
                await db.execute(
                    "SELECT COUNT(DISTINCT task_id) FROM working_memory WHERE last_seen_at >= ?",
                    (cutoff,),
                )
            ).fetchone()
            self._cached_working_memory_active_tasks = int(row[0]) if row else 0

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
        if _HAS_TELEMETRY and _lithos_metrics is not None:
            _lithos_metrics.lcma_salience_updates.add(1)

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

    async def update_last_decay_applied_at(self, node_id: str) -> None:
        """Set ``last_decay_applied_at`` to now for *node_id*."""
        await self._ensure_open()
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE node_stats SET last_decay_applied_at = ? WHERE node_id = ?",
                (now, node_id),
            )
            await db.commit()

    async def update_last_used_at(self, node_id: str) -> None:
        """Set ``last_used_at`` to now for *node_id*.

        Creates the row with default salience if absent.
        """
        await self._ensure_open()
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO node_stats (node_id, last_used_at)
                   VALUES (?, ?)
                   ON CONFLICT(node_id) DO UPDATE SET
                     last_used_at = excluded.last_used_at""",
                (node_id, now),
            )
            await db.commit()

    # ------------------------------------------------------------------
    # Consolidation tracking operations
    # ------------------------------------------------------------------

    async def is_task_consolidated(self, task_id: str) -> bool:
        """Return ``True`` if *task_id* exists in ``task_consolidation_log``."""
        await self._ensure_open()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM task_consolidation_log WHERE task_id = ?",
                (task_id,),
            )
            return await cursor.fetchone() is not None

    async def mark_task_consolidated(self, task_id: str) -> None:
        """Insert *task_id* into ``task_consolidation_log`` (INSERT OR IGNORE)."""
        await self._ensure_open()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO task_consolidation_log (task_id) VALUES (?)",
                (task_id,),
            )
            await db.commit()

    async def has_consolidation_edge_op(self, task_id: str, from_id: str, to_id: str) -> bool:
        """Return ``True`` if the edge op has already been recorded."""
        await self._ensure_open()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM consolidation_edge_ops "
                "WHERE task_id = ? AND from_id = ? AND to_id = ?",
                (task_id, from_id, to_id),
            )
            return await cursor.fetchone() is not None

    async def record_consolidation_edge_op(self, task_id: str, from_id: str, to_id: str) -> None:
        """Record an edge consolidation op (INSERT OR IGNORE)."""
        await self._ensure_open()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO consolidation_edge_ops (task_id, from_id, to_id) "
                "VALUES (?, ?, ?)",
                (task_id, from_id, to_id),
            )
            await db.commit()

    async def has_consolidation_salience_op(self, task_id: str, node_id: str) -> bool:
        """Return ``True`` if the salience op has already been recorded."""
        await self._ensure_open()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM consolidation_salience_ops WHERE task_id = ? AND node_id = ?",
                (task_id, node_id),
            )
            return await cursor.fetchone() is not None

    async def record_consolidation_salience_op(self, task_id: str, node_id: str) -> None:
        """Record a salience consolidation op (INSERT OR IGNORE)."""
        await self._ensure_open()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO consolidation_salience_ops (task_id, node_id) VALUES (?, ?)",
                (task_id, node_id),
            )
            await db.commit()

    async def update_salience_and_record_consolidation(
        self, *, node_id: str, delta: float, task_id: str
    ) -> None:
        """Atomically update salience and record the consolidation salience op.

        Both writes happen in a single stats.db transaction so that a crash
        between them cannot cause double-boosting on retry.
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
            await db.execute(
                "INSERT OR IGNORE INTO consolidation_salience_ops (task_id, node_id) VALUES (?, ?)",
                (task_id, node_id),
            )
            await db.commit()

    # ------------------------------------------------------------------
    # Receipt lookup operations
    # ------------------------------------------------------------------

    async def get_receipt(self, receipt_id: str, task_id: str) -> dict[str, object] | None:
        """Look up a receipt by ID and verify it belongs to *task_id*.

        Returns the receipt row as a dict with an added ``final_node_ids``
        field (``list[str]``), or ``None`` if no matching receipt exists or
        the task_id does not match.
        """
        await self._ensure_open()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
            row = await cursor.fetchone()
        if row is None:
            return None
        result = dict(row)
        if result.get("task_id") != task_id:
            return None
        result["final_node_ids"] = _extract_final_node_ids(result.get("final_nodes", "[]"))
        return result

    async def get_latest_receipt(self, task_id: str, agent_id: str) -> dict[str, object] | None:
        """Return the most recent receipt for *(task_id, agent_id)*.

        Uses ``ORDER BY rowid DESC LIMIT 1`` for recency. Returns the
        receipt row as a dict with an added ``final_node_ids`` field, or
        ``None`` if no receipt matches.
        """
        await self._ensure_open()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM receipts WHERE task_id = ? AND agent_id = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (task_id, agent_id),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        result = dict(row)
        result["final_node_ids"] = _extract_final_node_ids(result.get("final_nodes", "[]"))
        return result

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
    async def _migrate_add_last_decay_applied_at(db: aiosqlite.Connection) -> None:
        """Add last_decay_applied_at column to existing node_stats tables."""
        cursor = await db.execute("PRAGMA table_info(node_stats)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "last_decay_applied_at" not in columns:
            await db.execute("ALTER TABLE node_stats ADD COLUMN last_decay_applied_at TIMESTAMP")

    @staticmethod
    async def _migrate_add_enrich_queue_attempts(db: aiosqlite.Connection) -> None:
        """Add attempts column to existing enrich_queue tables."""
        cursor = await db.execute("PRAGMA table_info(enrich_queue)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "attempts" not in columns:
            await db.execute(
                "ALTER TABLE enrich_queue ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
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
