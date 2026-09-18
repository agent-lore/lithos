"""Agent registry — the ``agents`` table of coordination.db.

Split out of ``coordination.py`` (which owns tasks, claims, findings and the
connection lifecycle) so the registry can grow without breaching the module
line budget. :class:`CoordinationService` keeps thin delegators with the
original signatures, so callers never touch this class directly; it shares
coordination.db and is initialised on the same connection, inside the same
transaction, as the rest of the schema.
"""

import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from lithos.errors import CoordinationError
from lithos.sqlite_datetime import format_datetime, parse_datetime

logger = logging.getLogger(__name__)

# DDL for the registry, applied statement-by-statement by ``ensure_schema``
# (never via ``executescript``, which would implicitly commit the caller's
# open transaction).
AGENTS_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS agents (
        id TEXT PRIMARY KEY,
        name TEXT,
        type TEXT,
        first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        metadata JSON,
        archived_at TIMESTAMP
    )
    """,
    # Case-insensitive name lookup (#423). An expression index so the
    # collision check is a SEARCH, never a table scan — pinned by an
    # EXPLAIN QUERY PLAN test.
    "CREATE INDEX IF NOT EXISTS idx_agents_name_lower ON agents(lower(name))",
)

# Name-collision probe (#423). Bind the *raw* name: SQLite's lower() folds
# ASCII only, so pre-folding with Python's Unicode-aware str.lower() would
# make non-ASCII names miss the index. NULL names never match. No LIMIT: the
# contract is "every other active agent with this name", and the index
# bounds the cost by the number of genuine collisions, not the table.
# Public so the query-plan test needn't import a private name.
AGENT_NAME_COLLISION_SQL = (
    "SELECT * FROM agents "
    "WHERE lower(name) = lower(?) AND archived_at IS NULL AND id != ? "
    "ORDER BY last_seen_at DESC"
)


@dataclass
class Agent:
    """Agent information."""

    id: str
    name: str | None = None
    type: str | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    archived_at: datetime | None = None


class AgentRegistry:
    """Registry operations over the ``agents`` table.

    ``db_path`` is a provider, not a path: ``CoordinationService.db_path`` is a
    property that tests override after construction, and every operation
    opens its own short-lived connection, so the path is resolved per call.
    """

    def __init__(self, db_path: Callable[[], Path]) -> None:
        self._db_path = db_path

    @property
    def db_path(self) -> Path:
        """Resolve the coordination.db path via the provider."""
        return self._db_path()

    async def ensure_schema(self, db: aiosqlite.Connection) -> None:
        """Apply the registry DDL and migrations on the caller's connection.
        Does not commit."""
        for statement in AGENTS_DDL:
            await db.execute(statement)
        await self._migrate_agents_add_archived_at(db)

    @staticmethod
    async def _migrate_agents_add_archived_at(db: aiosqlite.Connection) -> None:
        """Add ``agents.archived_at`` (#423) to a pre-existing table.

        Idempotent: guarded by ``PRAGMA table_info``. Nullable with no
        default, so every existing row starts active. The lower(name) index
        in ``AGENTS_DDL`` needs no migration — ``CREATE INDEX IF NOT EXISTS``
        is safe on a legacy table.
        """
        cursor = await db.execute("PRAGMA table_info(agents)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "archived_at" in columns:
            return
        await db.execute("ALTER TABLE agents ADD COLUMN archived_at TIMESTAMP")
        logger.info("coordination.db migration applied: added agents.archived_at")

    async def ensure_agent_known(self, agent_id: str) -> None:
        """Ensure agent is registered, auto-registering if needed.

        Any activity resurrects an archived agent (#423): archived means
        "no activity since archiving", so the stamp bump also clears
        ``archived_at``.
        """
        logger.debug("ensure_agent_known: agent_id=%s", agent_id)
        now = format_datetime(datetime.now(UTC))
        async with aiosqlite.connect(self.db_path) as db:
            # Try to update last_seen_at
            cursor = await db.execute(
                "UPDATE agents SET last_seen_at = ?, archived_at = NULL WHERE id = ?",
                (now, agent_id),
            )
            if cursor.rowcount == 0:
                # Agent doesn't exist, insert
                await db.execute(
                    "INSERT INTO agents (id, first_seen_at, last_seen_at) VALUES (?, ?, ?)",
                    (agent_id, now, now),
                )
            await db.commit()

    async def register_agent(
        self,
        agent_id: str,
        name: str | None = None,
        agent_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[bool, bool]:
        """Register or update an agent.

        Args:
            agent_id: Agent identifier
            name: Human-friendly name
            agent_type: Agent type
            metadata: Additional metadata

        Returns:
            Tuple of (success, created)
        """
        now = format_datetime(datetime.now(UTC))
        metadata_json = json.dumps(metadata) if metadata else None

        async with aiosqlite.connect(self.db_path) as db:
            # Check if exists
            cursor = await db.execute(
                "SELECT id FROM agents WHERE id = ?",
                (agent_id,),
            )
            exists = await cursor.fetchone() is not None

            if exists:
                # Update existing
                await db.execute(
                    """
                    UPDATE agents
                    SET name = COALESCE(?, name),
                        type = COALESCE(?, type),
                        metadata = COALESCE(?, metadata),
                        last_seen_at = ?,
                        archived_at = NULL
                    WHERE id = ?
                    """,
                    (name, agent_type, metadata_json, now, agent_id),
                )
            else:
                # Insert new
                await db.execute(
                    """
                    INSERT INTO agents (id, name, type, metadata, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (agent_id, name, agent_type, metadata_json, now, now),
                )

            await db.commit()
            created = not exists
            if created:
                logger.info(
                    "Agent registered: agent_id=%s name=%s type=%s",
                    agent_id,
                    name,
                    agent_type,
                    extra={"agent_id": agent_id, "agent_name": name, "agent_type": agent_type},
                )
            else:
                logger.debug(
                    "Agent updated: agent_id=%s name=%s type=%s",
                    agent_id,
                    name,
                    agent_type,
                )
            return True, created

    async def get_agent(self, agent_id: str) -> Agent | None:
        """Get agent information."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM agents WHERE id = ?",
                (agent_id,),
            )
            row = await cursor.fetchone()

            if not row:
                return None
            return self._row_to_agent(row)

    async def list_agents(
        self,
        agent_type: str | None = None,
        active_since: datetime | None = None,
        *,
        include_archived: bool = False,
    ) -> list[Agent]:
        """List known agents, most recently active first.

        Archived agents are hidden unless ``include_archived`` is set.
        """
        query = "SELECT * FROM agents WHERE 1=1"
        params: list[Any] = []

        if not include_archived:
            query += " AND archived_at IS NULL"

        if agent_type:
            query += " AND type = ?"
            params.append(agent_type)

        if active_since:
            query += " AND last_seen_at >= ?"
            params.append(format_datetime(active_since))

        query += " ORDER BY last_seen_at DESC"

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
            return [self._row_to_agent(row) for row in rows]

    async def archive_agent(self, agent_id: str) -> tuple[Agent, bool]:
        """Archive an agent (#423): it keeps its history and ``agent_info``,
        but leaves ``list_agents`` until it is next active.

        Idempotent — an already-archived agent keeps its original stamp.
        Does not touch ``last_seen_at``: archiving is the archiver's
        activity, not the target's.

        The active→archived transition is one conditional UPDATE, so
        concurrent calls agree: exactly one sees ``newly_archived`` and all
        of them return the winner's stamp.

        Returns:
            ``(agent, newly_archived)``

        Raises:
            CoordinationError: ``agent_not_found`` when no such agent exists.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "UPDATE agents SET archived_at = ? WHERE id = ? AND archived_at IS NULL",
                (format_datetime(datetime.now(UTC)), agent_id),
            )
            newly = cursor.rowcount == 1

            # rowcount 0 is either "already archived" or "no such agent"; the
            # row settles it and carries the winner's stamp. Read it *before*
            # committing: the write lock keeps a reactivation by the target
            # from landing between the transition and this snapshot.
            cursor = await db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,))
            row = await cursor.fetchone()
            await db.commit()
            if row is None:
                raise CoordinationError("agent_not_found", f"Agent {agent_id!r} not found")
            if newly:
                logger.info("Agent archived: agent_id=%s", agent_id, extra={"agent_id": agent_id})
            return self._row_to_agent(row), newly

    async def find_name_collisions(self, name: str | None, *, exclude_id: str) -> list[Agent]:
        """Active agents (other than ``exclude_id``) whose ``name`` matches
        case-insensitively. Blank or missing names never collide."""
        if name is None or not name.strip():
            return []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(AGENT_NAME_COLLISION_SQL, (name, exclude_id))
            return [self._row_to_agent(row) for row in await cursor.fetchall()]

    @staticmethod
    def _row_to_agent(row: aiosqlite.Row) -> Agent:
        """Decode one ``agents`` row; unreadable metadata degrades to ``{}``."""
        metadata = {}
        if row["metadata"]:
            with contextlib.suppress(json.JSONDecodeError):
                metadata = json.loads(row["metadata"])

        return Agent(
            id=row["id"],
            name=row["name"],
            type=row["type"],
            first_seen_at=parse_datetime(row["first_seen_at"]),
            last_seen_at=parse_datetime(row["last_seen_at"]),
            metadata=metadata,
            archived_at=parse_datetime(row["archived_at"]),
        )
