"""Coordination service - SQLite-based tasks, claims, agents, findings."""

import contextlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from lithos.config import LithosConfig, get_config
from lithos.telemetry import lithos_metrics, traced

# SQL Schema
SCHEMA = """
-- Agent registry
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT,
    type TEXT,
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    metadata JSON
);

-- Tasks
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'open',
    created_by TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    tags JSON
);

-- Claims (with automatic expiry)
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    aspect TEXT NOT NULL,
    claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    UNIQUE(task_id, aspect)
);

-- Findings
CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    summary TEXT NOT NULL,
    knowledge_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_claims_task_id ON claims(task_id);
CREATE INDEX IF NOT EXISTS idx_claims_expires_at ON claims(expires_at);
CREATE INDEX IF NOT EXISTS idx_findings_task_id ON findings(task_id);
"""


@dataclass
class Agent:
    """Agent information."""

    id: str
    name: str | None = None
    type: str | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Task:
    """A coordination task."""

    id: str
    title: str
    description: str | None = None
    status: Literal["open", "completed", "cancelled"] = "open"
    created_by: str = ""
    created_at: datetime | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class Claim:
    """A task aspect claim."""

    task_id: str
    agent: str
    aspect: str
    claimed_at: datetime
    expires_at: datetime

    @property
    def is_expired(self) -> bool:
        """Check if claim is expired."""
        return datetime.now(timezone.utc) > self.expires_at


@dataclass
class Finding:
    """A task finding."""

    id: str
    task_id: str
    agent: str
    summary: str
    knowledge_id: str | None = None
    created_at: datetime | None = None


@dataclass
class TaskStatus:
    """Task status with claims."""

    id: str
    title: str
    status: str
    claims: list[Claim]


def _parse_datetime(value: str | datetime | None) -> datetime | None:
    """Parse datetime from SQLite."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        # SQLite stores as ISO format string
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _format_datetime(dt: datetime) -> str:
    """Format datetime for SQLite."""
    return dt.isoformat()


class CoordinationService:
    """SQLite-based coordination service."""

    def __init__(self, config: LithosConfig | None = None):
        """Initialize coordination service.

        Args:
            config: Configuration. Uses global config if not provided.
        """
        self._config = config
        self._db_path: Path | None = None

    @property
    def config(self) -> LithosConfig:
        """Get configuration."""
        return self._config or get_config()

    @property
    def db_path(self) -> Path:
        """Get database path."""
        if self._db_path:
            return self._db_path
        return self.config.storage.coordination_db_path

    async def initialize(self) -> None:
        """Initialize database schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # One-time migration: move coordination.db from old root location to .lithos/
        old_path = self.config.storage.data_dir / "coordination.db"
        if old_path.exists() and not self.db_path.exists() and old_path != self.db_path:
            old_path.rename(self.db_path)

        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def _get_db(self) -> aiosqlite.Connection:
        """Get database connection."""
        return await aiosqlite.connect(self.db_path)

    # ==================== Agent Operations ====================

    async def ensure_agent_known(self, agent_id: str) -> None:
        """Ensure agent is registered, auto-registering if needed."""
        now = _format_datetime(datetime.now(timezone.utc))
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
        import json

        now = _format_datetime(datetime.now(timezone.utc))
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
            return True, not exists

    async def get_agent(self, agent_id: str) -> Agent | None:
        """Get agent information."""
        import json

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM agents WHERE id = ?",
                (agent_id,),
            )
            row = await cursor.fetchone()

            if not row:
                return None

            metadata = {}
            if row["metadata"]:
                with contextlib.suppress(json.JSONDecodeError):
                    metadata = json.loads(row["metadata"])

            return Agent(
                id=row["id"],
                name=row["name"],
                type=row["type"],
                first_seen_at=_parse_datetime(row["first_seen_at"]),
                last_seen_at=_parse_datetime(row["last_seen_at"]),
                metadata=metadata,
            )

    async def list_agents(
        self,
        agent_type: str | None = None,
        active_since: datetime | None = None,
    ) -> list[Agent]:
        """List all known agents."""
        import json

        query = "SELECT * FROM agents WHERE 1=1"
        params: list[Any] = []

        if agent_type:
            query += " AND type = ?"
            params.append(agent_type)

        if active_since:
            query += " AND last_seen_at >= ?"
            params.append(_format_datetime(active_since))

        query += " ORDER BY last_seen_at DESC"

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()

            agents = []
            for row in rows:
                metadata = {}
                if row["metadata"]:
                    with contextlib.suppress(json.JSONDecodeError):
                        metadata = json.loads(row["metadata"])

                agents.append(
                    Agent(
                        id=row["id"],
                        name=row["name"],
                        type=row["type"],
                        first_seen_at=_parse_datetime(row["first_seen_at"]),
                        last_seen_at=_parse_datetime(row["last_seen_at"]),
                        metadata=metadata,
                    )
                )

            return agents

    # ==================== Task Operations ====================

    @traced("lithos.coordination.create_task")
    async def create_task(
        self,
        title: str,
        agent: str,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Create a new task.

        Returns:
            Task ID
        """
        import json

        lithos_metrics.coordination_ops.add(1, {"op": "create_task"})
        await self.ensure_agent_known(agent)

        task_id = str(uuid.uuid4())
        tags_json = json.dumps(tags) if tags else None
        now = _format_datetime(datetime.now(timezone.utc))

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO tasks (id, title, description, created_by, tags, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_id, title, description, agent, tags_json, now),
            )
            await db.commit()

        return task_id

    async def get_task(self, task_id: str) -> Task | None:
        """Get task by ID."""
        import json

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            )
            row = await cursor.fetchone()

            if not row:
                return None

            tags = []
            if row["tags"]:
                with contextlib.suppress(json.JSONDecodeError):
                    tags = json.loads(row["tags"])

            return Task(
                id=row["id"],
                title=row["title"],
                description=row["description"],
                status=row["status"],
                created_by=row["created_by"],
                created_at=_parse_datetime(row["created_at"]),
                tags=tags,
            )

    @traced("lithos.coordination.update_task")
    async def update_task(
        self,
        task_id: str,
        agent: str,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> bool:
        """Update mutable task metadata.

        Only updates fields that are not None (partial update pattern).
        Only open tasks can be updated; completed or cancelled tasks are
        treated as not found (consistent with complete_task behaviour).

        Returns:
            True if task was found, is open, and was updated; False otherwise
        """
        import json

        lithos_metrics.coordination_ops.add(1, {"op": "update_task"})
        await self.ensure_agent_known(agent)

        sets: list[str] = []
        params: list[Any] = []

        if title is not None:
            sets.append("title = ?")
            params.append(title)
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        if tags is not None:
            sets.append("tags = ?")
            params.append(json.dumps(tags))

        if not sets:
            # Nothing to update; check task exists and is open
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT id FROM tasks WHERE id = ? AND status = 'open'", (task_id,)
                )
                return await cursor.fetchone() is not None

        params.append(task_id)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id = ? AND status = 'open'",
                params,
            )
            await db.commit()
            return cursor.rowcount > 0

    @traced("lithos.coordination.complete_task")
    async def complete_task(self, task_id: str, agent: str) -> bool:
        """Mark task as completed and release all claims.

        Returns:
            True if task was completed
        """
        lithos_metrics.coordination_ops.add(1, {"op": "complete"})
        await self.ensure_agent_known(agent)

        async with aiosqlite.connect(self.db_path) as db:
            # Update task status
            cursor = await db.execute(
                "UPDATE tasks SET status = 'completed' WHERE id = ? AND status = 'open'",
                (task_id,),
            )
            if cursor.rowcount == 0:
                return False

            # Release all claims
            await db.execute(
                "DELETE FROM claims WHERE task_id = ?",
                (task_id,),
            )

            await db.commit()
            return True

    @traced("lithos.coordination.cancel_task")
    async def cancel_task(self, task_id: str, agent: str, reason: str | None = None) -> bool:
        """Mark task as cancelled and release all claims.

        Returns:
            True if task was cancelled
        """
        lithos_metrics.coordination_ops.add(1, {"op": "cancel"})
        await self.ensure_agent_known(agent)

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE tasks SET status = 'cancelled' WHERE id = ? AND status = 'open'",
                (task_id,),
            )
            if cursor.rowcount == 0:
                return False

            await db.execute(
                "DELETE FROM claims WHERE task_id = ?",
                (task_id,),
            )

            await db.commit()
            return True

    async def list_tasks(
        self,
        agent: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        """List tasks with optional filters.

        Args:
            agent: Filter by created_by agent
            status: Filter by status (open/completed/cancelled), or None for all
            tags: Filter by tags (task must have all specified tags)
            since: Filter by created_at >= this ISO datetime string

        Returns:
            List of task dicts with id, title, description, status, created_by, created_at, tags
        """
        import json

        query = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []

        if agent:
            query += " AND created_by = ?"
            params.append(agent)

        if status:
            query += " AND status = ?"
            params.append(status)

        if since:
            query += " AND created_at >= ?"
            params.append(since)

        query += " ORDER BY created_at DESC"

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()

            results: list[dict[str, Any]] = []
            for row in rows:
                task_tags: list[str] = []
                if row["tags"]:
                    with contextlib.suppress(json.JSONDecodeError):
                        task_tags = json.loads(row["tags"])

                # Filter by tags: task must contain all requested tags
                if tags and not all(t in task_tags for t in tags):
                    continue

                results.append(
                    {
                        "id": row["id"],
                        "title": row["title"],
                        "description": row["description"],
                        "status": row["status"],
                        "created_by": row["created_by"],
                        "created_at": row["created_at"],
                        "tags": task_tags,
                    }
                )

            return results

    async def get_task_status(
        self,
        task_id: str | None = None,
        include_all: bool = False,
    ) -> list[TaskStatus]:
        """Get task status with active claims.

        Args:
            task_id: Specific task ID, or None for all active tasks
            include_all: When True and task_id is None, include non-open tasks
        """
        now = _format_datetime(datetime.now(timezone.utc))

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            if task_id:
                cursor = await db.execute(
                    "SELECT * FROM tasks WHERE id = ?",
                    (task_id,),
                )
            else:
                if include_all:
                    cursor = await db.execute("SELECT * FROM tasks")
                else:
                    cursor = await db.execute("SELECT * FROM tasks WHERE status = 'open'")

            tasks = await cursor.fetchall()
            result: list[TaskStatus] = []

            for task in tasks:
                # Get active (non-expired) claims
                claims_cursor = await db.execute(
                    """
                    SELECT * FROM claims
                    WHERE task_id = ? AND expires_at > ?
                    """,
                    (task["id"], now),
                )
                claim_rows = await claims_cursor.fetchall()

                claims = [
                    Claim(
                        task_id=row["task_id"],
                        agent=row["agent"],
                        aspect=row["aspect"],
                        claimed_at=_parse_datetime(row["claimed_at"]) or datetime.now(timezone.utc),
                        expires_at=_parse_datetime(row["expires_at"]) or datetime.now(timezone.utc),
                    )
                    for row in claim_rows
                ]

                result.append(
                    TaskStatus(
                        id=task["id"],
                        title=task["title"],
                        status=task["status"],
                        claims=claims,
                    )
                )

            return result

    # ==================== Claim Operations ====================

    @traced("lithos.coordination.claim_task")
    async def claim_task(
        self,
        task_id: str,
        aspect: str,
        agent: str,
        ttl_minutes: int = 60,
    ) -> tuple[bool, datetime | None]:
        """Claim an aspect of a task.

        Args:
            task_id: Task ID
            aspect: Aspect being claimed
            agent: Agent making the claim
            ttl_minutes: Claim duration in minutes

        Returns:
            Tuple of (success, expires_at)
        """
        lithos_metrics.coordination_ops.add(1, {"op": "claim"})
        await self.ensure_agent_known(agent)

        # Clamp TTL
        max_ttl = self.config.coordination.claim_max_ttl_minutes
        ttl_minutes = max(1, min(ttl_minutes, max_ttl))

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=ttl_minutes)

        async with aiosqlite.connect(self.db_path) as db:
            # Check if task exists and is open
            cursor = await db.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (task_id,),
            )
            task = await cursor.fetchone()
            if not task or task[0] != "open":
                return False, None

            # Atomically insert or update the claim in a single statement.
            # The DO UPDATE WHERE clause only fires when the existing claim is
            # expired (expires_at <= now) OR belongs to the same agent (renewal).
            # When an active claim held by a different agent exists the WHERE is
            # false, the row is left unchanged, and changes() returns 0 — closing
            # the SELECT-then-write TOCTOU gap.
            cursor = await db.execute(
                """
                INSERT INTO claims (task_id, agent, aspect, claimed_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id, aspect) DO UPDATE SET
                    agent      = excluded.agent,
                    claimed_at = excluded.claimed_at,
                    expires_at = excluded.expires_at
                WHERE claims.expires_at <= ? OR claims.agent = excluded.agent
                """,
                (
                    task_id,
                    agent,
                    aspect,
                    _format_datetime(now),
                    _format_datetime(expires_at),
                    _format_datetime(now),
                ),
            )
            await db.commit()
            if cursor.rowcount == 1:
                return True, expires_at
            return False, None

    async def renew_claim(
        self,
        task_id: str,
        aspect: str,
        agent: str,
        ttl_minutes: int = 60,
    ) -> tuple[bool, datetime | None]:
        """Renew an existing claim.

        Returns:
            Tuple of (success, new_expires_at)
        """
        await self.ensure_agent_known(agent)

        max_ttl = self.config.coordination.claim_max_ttl_minutes
        ttl_minutes = max(1, min(ttl_minutes, max_ttl))

        now = datetime.now(timezone.utc)
        new_expires = now + timedelta(minutes=ttl_minutes)

        async with aiosqlite.connect(self.db_path) as db:
            # Check claim ownership
            cursor = await db.execute(
                """
                SELECT agent FROM claims
                WHERE task_id = ? AND aspect = ? AND expires_at > ?
                """,
                (task_id, aspect, _format_datetime(now)),
            )
            row = await cursor.fetchone()

            if not row:
                return False, None  # No active claim

            if row[0] != agent:
                return False, None  # Not owned by this agent

            # Update expiry
            await db.execute(
                """
                UPDATE claims SET expires_at = ?
                WHERE task_id = ? AND aspect = ?
                """,
                (_format_datetime(new_expires), task_id, aspect),
            )
            await db.commit()
            return True, new_expires

    @traced("lithos.coordination.release_claim")
    async def release_claim(
        self,
        task_id: str,
        aspect: str,
        agent: str,
    ) -> bool:
        """Release a claim.

        Returns:
            True if claim was released
        """
        await self.ensure_agent_known(agent)

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                DELETE FROM claims
                WHERE task_id = ? AND aspect = ? AND agent = ?
                """,
                (task_id, aspect, agent),
            )
            await db.commit()
            return cursor.rowcount > 0

    # ==================== Finding Operations ====================

    async def post_finding(
        self,
        task_id: str,
        agent: str,
        summary: str,
        knowledge_id: str | None = None,
    ) -> str:
        """Post a finding to a task.

        Returns:
            Finding ID
        """
        await self.ensure_agent_known(agent)

        finding_id = str(uuid.uuid4())
        now = _format_datetime(datetime.now(timezone.utc))

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO findings (id, task_id, agent, summary, knowledge_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (finding_id, task_id, agent, summary, knowledge_id, now),
            )
            await db.commit()

        return finding_id

    async def list_findings(
        self,
        task_id: str,
        since: datetime | None = None,
    ) -> list[Finding]:
        """List findings for a task."""
        query = "SELECT * FROM findings WHERE task_id = ?"
        params: list[Any] = [task_id]

        if since:
            query += " AND created_at > ?"
            params.append(_format_datetime(since))

        query += " ORDER BY created_at ASC"

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()

            return [
                Finding(
                    id=row["id"],
                    task_id=row["task_id"],
                    agent=row["agent"],
                    summary=row["summary"],
                    knowledge_id=row["knowledge_id"],
                    created_at=_parse_datetime(row["created_at"]),
                )
                for row in rows
            ]

    # ==================== Statistics ====================

    async def get_stats(self) -> dict[str, int]:
        """Get coordination statistics."""
        now = _format_datetime(datetime.now(timezone.utc))

        async with aiosqlite.connect(self.db_path) as db:
            # Count agents
            cursor = await db.execute("SELECT COUNT(*) FROM agents")
            row = await cursor.fetchone()
            agents = row[0] if row else 0

            # Count active tasks
            cursor = await db.execute("SELECT COUNT(*) FROM tasks WHERE status = 'open'")
            row = await cursor.fetchone()
            active_tasks = row[0] if row else 0

            # Count active claims
            cursor = await db.execute(
                "SELECT COUNT(*) FROM claims WHERE expires_at > ?",
                (now,),
            )
            row = await cursor.fetchone()
            open_claims = row[0] if row else 0

            return {
                "agents": agents,
                "active_tasks": active_tasks,
                "open_claims": open_claims,
            }
