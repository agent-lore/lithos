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
        metadata JSON
    )
    """,
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
        """Apply the registry DDL on the caller's connection. Does not commit."""
        for statement in AGENTS_DDL:
            await db.execute(statement)

    async def ensure_agent_known(self, agent_id: str) -> None:
        """Ensure agent is registered, auto-registering if needed."""
        logger.debug("ensure_agent_known: agent_id=%s", agent_id)
        now = format_datetime(datetime.now(UTC))
        async with aiosqlite.connect(self.db_path) as db:
            # Try to update last_seen_at
            cursor = await db.execute(
                "UPDATE agents SET last_seen_at = ? WHERE id = ?",
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
                        last_seen_at = ?
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
    ) -> list[Agent]:
        """List all known agents."""
        query = "SELECT * FROM agents WHERE 1=1"
        params: list[Any] = []

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
        )
