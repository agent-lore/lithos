"""LCMA edge store — lazily-created SQLite database for typed edges.

Follows the coordination.db pattern: async via aiosqlite, single-writer safe,
corrupt-DB quarantine with automatic recreation.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from lithos.config import LithosConfig, get_config

if TYPE_CHECKING:
    from lithos.knowledge import KnowledgeManager

logger = logging.getLogger(__name__)

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


def _generate_edge_id() -> str:
    """Generate a short edge ID in the form ``edge_<short-uuid>``."""
    return f"edge_{uuid.uuid4().hex[:12]}"


class EdgeStore:
    """Lazily-created SQLite store for LCMA typed edges.

    The database file is created on the first call to :meth:`open`.
    Corrupt databases are quarantined (renamed) and recreated with an
    empty schema.
    """

    def __init__(self, config: LithosConfig | None = None) -> None:
        self._config = config

    @property
    def config(self) -> LithosConfig:
        return self._config or get_config()

    @property
    def db_path(self) -> Path:
        return self.config.storage.edges_db_path

    async def open(self) -> None:
        """Ensure edges.db exists with the correct schema.

        Idempotent — safe to call multiple times.  If the file is corrupt
        it is quarantined and a fresh database is created.
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        if self.db_path.exists():
            healthy = await self._probe(self.db_path)
            if not healthy:
                self._quarantine(self.db_path)

        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _probe(path: Path) -> bool:
        """Return True if *path* is a usable SQLite database."""
        try:
            async with aiosqlite.connect(path) as db:
                # integrity_check actually reads the file, unlike SELECT 1
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
        logger.warning("Quarantined corrupt edges.db → %s", backup)
        return backup

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
        """Insert or update an edge.  Returns the ``edge_id``."""
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            # Check for existing edge by composite key
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
            await db.commit()
        return edge_id

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

        async with aiosqlite.connect(self.db_path) as db:
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
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(sql, params)
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def delete_edges(self, *, edge_ids: list[str]) -> int:
        """Delete edges by their IDs.  Returns the number of rows deleted."""
        if not edge_ids:
            return 0
        placeholders = ",".join("?" for _ in edge_ids)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                f"DELETE FROM edges WHERE edge_id IN ({placeholders})", edge_ids
            )
            await db.commit()
            return cursor.rowcount


async def _project_provenance_to_edges(
    edge_store: EdgeStore,
    knowledge: KnowledgeManager,
) -> dict[str, int]:
    """Project ``derived_from_ids`` from frontmatter into edges.db.

    For every document that has ``derived_from_ids``, ensures a
    ``type='derived_from'`` edge exists from the document to each source.
    Removes orphan ``derived_from`` edges that no longer correspond to any
    document's frontmatter.

    Returns ``{"created": N, "removed": M}`` summarising changes.

    No-op (returns ``{"created": 0, "removed": 0}``) when edges.db does not
    exist on disk.
    """
    if not edge_store.db_path.exists():
        return {"created": 0, "removed": 0}

    from lithos.knowledge import derive_namespace

    # Build the desired set of (from_id, to_id, namespace) from frontmatter
    desired: set[tuple[str, str, str]] = set()
    for doc_id, sources in knowledge._doc_to_sources.items():
        if not sources:
            continue
        cached = knowledge._meta_cache.get(doc_id)
        ns = derive_namespace(cached.path) if cached else "default"
        for source_id in sources:
            desired.add((doc_id, source_id, ns))

    # Read existing derived_from edges from edges.db
    existing_edges = await edge_store.list_edges(edge_type="derived_from")
    existing_map: dict[tuple[str, str, str], str] = {}
    for e in existing_edges:
        key = (str(e["from_id"]), str(e["to_id"]), str(e["namespace"]))
        existing_map[key] = str(e["edge_id"])

    existing_keys = set(existing_map.keys())

    # Create missing edges
    to_create = desired - existing_keys
    created = 0
    for from_id, to_id, ns in to_create:
        await edge_store.upsert(
            from_id=from_id,
            to_id=to_id,
            edge_type="derived_from",
            weight=1.0,
            namespace=ns,
            provenance_type="frontmatter",
        )
        created += 1

    # Remove orphan edges
    to_remove = existing_keys - desired
    orphan_ids = [existing_map[k] for k in to_remove]
    removed = await edge_store.delete_edges(edge_ids=orphan_ids)

    return {"created": created, "removed": removed}
