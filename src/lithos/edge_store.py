"""Lithos edge store — lazily-created SQLite database for typed edges.

Follows the coordination.db pattern: async via aiosqlite, single-writer safe,
corrupt-DB quarantine with automatic recreation.

This module is a public peer of :mod:`lithos.provenance` and
:mod:`lithos.intake` (ADR-0006 Slice 1). ``ProvenanceProjection`` owns the
projection-class edge rows (corpus-derived); ``CorpusIntake.assert_edge``
owns the asserted-class rows. Both share the same underlying
:class:`EdgeStore`, injected from ``LithosServer.initialize()`` so the
SQLite handle is opened exactly once per server.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from lithos.async_sqlite_store import AsyncSqliteStore

logger = logging.getLogger(__name__)


def _generate_edge_id() -> str:
    """Generate a short edge ID in the form ``edge_<short-uuid>``."""
    return f"edge_{uuid.uuid4().hex[:12]}"


class EdgeStore(AsyncSqliteStore):
    """Lazily-created SQLite store for typed edges.

    The database file is created on the first call to :meth:`open`. Corrupt
    databases are quarantined (renamed) and recreated with an empty schema; the
    connection lifecycle lives in
    :class:`~lithos.async_sqlite_store.AsyncSqliteStore`.
    """

    SCHEMA = """
CREATE TABLE IF NOT EXISTS edges (
    edge_id TEXT PRIMARY KEY,
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    type TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    namespace TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    provenance_actor TEXT,
    provenance_type TEXT,
    evidence TEXT,
    conflict_state TEXT,
    UNIQUE(from_id, to_id, type, namespace)
);

CREATE INDEX IF NOT EXISTS idx_edges_from_id ON edges(from_id);
CREATE INDEX IF NOT EXISTS idx_edges_to_id ON edges(to_id);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type);
CREATE INDEX IF NOT EXISTS idx_edges_namespace ON edges(namespace);
"""

    @property
    def db_path(self) -> Path:
        return self.config.storage.edges_db_path

    # ------------------------------------------------------------------
    # Public data access helpers (used by lithos_edge_upsert / list)
    # ------------------------------------------------------------------

    async def upsert(
        self,
        *,
        from_id: str,
        to_id: str,
        edge_type: str,
        weight: float,
        namespace: str,
        provenance_actor: str | None = None,
        provenance_type: str | None = None,
        evidence: str | None = None,
        conflict_state: str | None = None,
    ) -> str:
        """Insert or update an edge.  Returns the ``edge_id``.

        The SELECT-then-INSERT/UPDATE pattern needs cross-statement atomicity
        on the shared autocommit connection (#172) — without it, two
        concurrent upserts for the same (from_id, to_id, type, namespace)
        would both observe "no existing row" and race to INSERT, hitting
        the UNIQUE constraint. The transactional :meth:`_session` serialises
        the read-modify-write so either both calls observe a single row and
        both UPDATE, or one INSERTs and the next UPDATEs.
        """
        now = datetime.now(UTC).isoformat()
        async with self._session(transactional=True) as db:
            cursor = await db.execute(
                "SELECT edge_id FROM edges "
                "WHERE from_id = ? AND to_id = ? AND type = ? AND namespace = ?",
                (from_id, to_id, edge_type, namespace),
            )
            row = await cursor.fetchone()

            if row is not None:
                edge_id: str = row[0]
                await db.execute(
                    "UPDATE edges SET weight = ?, updated_at = ?, "
                    "provenance_actor = ?, provenance_type = ?, "
                    "evidence = ?, conflict_state = ? "
                    "WHERE edge_id = ?",
                    (
                        weight,
                        now,
                        provenance_actor,
                        provenance_type,
                        evidence,
                        conflict_state,
                        edge_id,
                    ),
                )
                logger.debug(
                    "edge upsert (update): edge_id=%s from=%s to=%s type=%s ns=%s weight=%.3f",
                    edge_id,
                    from_id,
                    to_id,
                    edge_type,
                    namespace,
                    weight,
                    extra={
                        "edge_id": edge_id,
                        "from_id": from_id,
                        "to_id": to_id,
                        "edge_type": edge_type,
                        "namespace": namespace,
                        "weight": weight,
                    },
                )
            else:
                edge_id = _generate_edge_id()
                await db.execute(
                    "INSERT INTO edges "
                    "(edge_id, from_id, to_id, type, weight, namespace, "
                    "created_at, updated_at, provenance_actor, provenance_type, "
                    "evidence, conflict_state) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        edge_id,
                        from_id,
                        to_id,
                        edge_type,
                        weight,
                        namespace,
                        now,
                        now,
                        provenance_actor,
                        provenance_type,
                        evidence,
                        conflict_state,
                    ),
                )
                logger.info(
                    "edge upsert (insert): edge_id=%s from=%s to=%s type=%s ns=%s weight=%.3f",
                    edge_id,
                    from_id,
                    to_id,
                    edge_type,
                    namespace,
                    weight,
                    extra={
                        "edge_id": edge_id,
                        "from_id": from_id,
                        "to_id": to_id,
                        "edge_type": edge_type,
                        "namespace": namespace,
                        "weight": weight,
                    },
                )
        return edge_id

    async def get_edge(self, edge_id: str) -> dict[str, object] | None:
        """Return a single edge by its ID, or ``None`` if not found."""
        async with self._session() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT edge_id, from_id, to_id, type, weight, namespace, "
                "created_at, updated_at, provenance_actor, provenance_type, "
                "evidence, conflict_state FROM edges WHERE edge_id = ?",
                (edge_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "edge_id": row["edge_id"],
            "from_id": row["from_id"],
            "to_id": row["to_id"],
            "type": row["type"],
            "weight": row["weight"],
            "namespace": row["namespace"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "provenance_actor": row["provenance_actor"],
            "provenance_type": row["provenance_type"],
            "evidence": row["evidence"],
            "conflict_state": row["conflict_state"],
        }

    async def update_conflict_resolution(
        self,
        edge_id: str,
        *,
        conflict_state: str,
        provenance_actor: str,
    ) -> bool:
        """Update conflict_state and provenance_actor on an existing edge.

        Returns ``True`` if the edge was found and updated, ``False`` otherwise.
        """
        now = datetime.now(UTC).isoformat()
        async with self._session() as db:
            cursor = await db.execute(
                "UPDATE edges SET conflict_state = ?, provenance_actor = ?, updated_at = ? "
                "WHERE edge_id = ?",
                (conflict_state, provenance_actor, now, edge_id),
            )
        return cursor.rowcount > 0

    async def list_edges(
        self,
        *,
        from_id: str | None = None,
        to_id: str | None = None,
        edge_type: str | None = None,
        namespace: str | None = None,
    ) -> list[dict[str, object]]:
        """Query edges by optional filter dimensions."""
        clauses: list[str] = []
        params: list[str] = []
        if from_id is not None:
            clauses.append("from_id = ?")
            params.append(from_id)
        if to_id is not None:
            clauses.append("to_id = ?")
            params.append(to_id)
        if edge_type is not None:
            clauses.append("type = ?")
            params.append(edge_type)
        if namespace is not None:
            clauses.append("namespace = ?")
            params.append(namespace)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT edge_id, from_id, to_id, type, weight, namespace, created_at, updated_at, provenance_actor, provenance_type, evidence, conflict_state FROM edges{where}"

        async with self._session() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()

        return [
            {
                "edge_id": r["edge_id"],
                "from_id": r["from_id"],
                "to_id": r["to_id"],
                "type": r["type"],
                "weight": r["weight"],
                "namespace": r["namespace"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "provenance_actor": r["provenance_actor"],
                "provenance_type": r["provenance_type"],
                "evidence": r["evidence"],
                "conflict_state": r["conflict_state"],
            }
            for r in rows
        ]

    async def count(self, *, namespace: str | None = None) -> int:
        """Return total edge count, optionally filtered by namespace."""
        if namespace is not None:
            sql = "SELECT COUNT(*) FROM edges WHERE namespace = ?"
            params: tuple[str, ...] = (namespace,)
        else:
            sql = "SELECT COUNT(*) FROM edges"
            params = ()
        async with self._session() as db:
            cursor = await db.execute(sql, params)
            row = await cursor.fetchone()
        return row[0] if row else 0

    async def delete_edges(self, *, edge_ids: list[str]) -> int:
        """Delete edges by their IDs.  Returns the number of rows deleted."""
        if not edge_ids:
            return 0
        placeholders = ",".join("?" for _ in edge_ids)
        async with self._session() as db:
            cursor = await db.execute(
                f"DELETE FROM edges WHERE edge_id IN ({placeholders})",
                edge_ids,
            )
            deleted = cursor.rowcount
        logger.info(
            "edge delete_edges: requested=%d deleted=%d",
            len(edge_ids),
            deleted,
            extra={"requested": len(edge_ids), "deleted": deleted},
        )
        return deleted

    async def adjust_weight(self, edge_id: str, delta: float) -> float | None:
        """Atomically adjust weight by *delta*, clamping to [0.0, 1.0].

        Returns the new weight, or ``None`` if the edge is not found. The
        UPDATE+read-back pair is bracketed in BEGIN/COMMIT inside
        :meth:`_session` so that the returned weight reflects this call's
        own write even when another coroutine adjusts the same edge
        concurrently (#172).
        """
        now = datetime.now(UTC).isoformat()
        async with self._session(transactional=True) as db:
            cursor = await db.execute(
                "UPDATE edges SET weight = MAX(0.0, MIN(1.0, weight + ?)), updated_at = ? "
                "WHERE edge_id = ?",
                (delta, now, edge_id),
            )
            if cursor.rowcount == 0:
                return None
            cursor = await db.execute("SELECT weight FROM edges WHERE edge_id = ?", (edge_id,))
            row = await cursor.fetchone()
        assert row is not None
        return row[0]

    async def list_edges_between(
        self,
        node_ids: list[str],
        *,
        edge_type: str | None = None,
        namespace: str | None = None,
    ) -> list[dict[str, object]]:
        """Return all edges where both from_id and to_id are in *node_ids*."""
        if not node_ids:
            return []
        placeholders = ", ".join("?" for _ in node_ids)
        clauses = [
            f"from_id IN ({placeholders})",
            f"to_id IN ({placeholders})",
        ]
        params: list[object] = [*node_ids, *node_ids]
        if edge_type is not None:
            clauses.append("type = ?")
            params.append(edge_type)
        if namespace is not None:
            clauses.append("namespace = ?")
            params.append(namespace)
        where = " AND ".join(clauses)
        async with self._session() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(f"SELECT * FROM edges WHERE {where}", params)
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]
