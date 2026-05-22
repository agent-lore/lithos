"""Coordination service - SQLite-based tasks, claims, agents, findings."""

import contextlib
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from lithos.config import LithosConfig, get_config
from lithos.telemetry import lithos_metrics, traced

logger = logging.getLogger(__name__)

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
    tags JSON,
    outcome TEXT,
    resolved_at TIMESTAMP,
    metadata JSON
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

-- Read access audit log
CREATE TABLE IF NOT EXISTS access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL DEFAULT 'unknown',
    doc_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('read', 'search_result')),
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_claims_task_id ON claims(task_id);
CREATE INDEX IF NOT EXISTS idx_claims_expires_at ON claims(expires_at);
CREATE INDEX IF NOT EXISTS idx_findings_task_id ON findings(task_id);
CREATE INDEX IF NOT EXISTS idx_access_log_agent_id ON access_log(agent_id);
CREATE INDEX IF NOT EXISTS idx_access_log_doc_id ON access_log(doc_id);
CREATE INDEX IF NOT EXISTS idx_access_log_timestamp ON access_log(timestamp);
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
    outcome: str | None = None
    resolved_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


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
    """Task status with claims.

    Carries the same persisted fields as :class:`Task` (modulo identity)
    plus the task's currently-active (non-expired) claims. Earlier revisions
    of this dataclass returned only ``id``, ``title``, ``status``, ``metadata``
    and ``claims``; consumers (lithos-loom) ended up doing an N+1 re-fetch
    via ``lithos_task_list`` just to recover the missing fields. The store
    already has them — surface them.
    """

    id: str
    title: str
    status: str
    claims: list[Claim]
    metadata: dict[str, Any] = field(default_factory=dict)
    description: str | None = None
    created_by: str = ""
    created_at: datetime | None = None
    tags: list[str] = field(default_factory=list)
    outcome: str | None = None
    resolved_at: datetime | None = None


@dataclass
class AccessLogEntry:
    """A single read-access audit log entry."""

    id: int
    agent_id: str
    doc_id: str
    operation: Literal["read", "search_result"]
    timestamp: datetime | None = None


def _parse_datetime(value: str | datetime | None) -> datetime | None:
    """Parse a datetime from SQLite.

    Returns ``None`` for legitimately-missing values (the field is NULL or
    already ``None``) and *also* for values the parser could not interpret.
    Unparseable values are logged at WARNING with the offending raw input so
    that silent data corruption (a partial write, a manual edit, a schema
    mismatch) shows up in operator logs rather than degrading silently to
    ``None`` and then to ``datetime.now()`` at the call site (#205).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        # SQLite stores as ISO format string
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        logger.warning(
            "Failed to parse datetime from SQLite value %r; treating as missing",
            value,
        )
        return None


def _format_datetime(dt: datetime) -> str:
    """Format datetime for SQLite."""
    return dt.isoformat()


def _decode_metadata(raw: Any) -> dict[str, Any]:
    """Decode a stored ``tasks.metadata`` JSON column into a dict, safely.

    The schema expects a JSON object, but real-world ``raw`` can be any
    of three problem shapes: ``NULL``/empty (legitimately missing), a
    non-string Python value produced by SQLite's loose type affinity
    (e.g. a row written with a bare numeric literal comes back as
    ``int``), or a valid JSON string whose top-level value isn't an
    object (``null``, arrays, scalars). Treat all three as ``{}`` so
    downstream consumers (``get_task``, the merge path) never see a
    non-dict for a field typed as ``dict[str, Any]``. Log a warning on
    the corruption cases — operators will want to know.
    """
    import json

    if raw is None or raw == "":
        return {}
    if not isinstance(raw, (str, bytes, bytearray)):
        logger.warning(
            "tasks.metadata stored as non-string %s; treating as empty: %r",
            type(raw).__name__,
            raw,
        )
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("tasks.metadata is not valid JSON; treating as empty: %r", raw)
        return {}
    if not isinstance(decoded, dict):
        logger.warning(
            "tasks.metadata is not a JSON object (got %s); treating as empty: %r",
            type(decoded).__name__,
            raw,
        )
        return {}
    return decoded


def _merge_metadata(existing: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Apply an additive per-key patch to a metadata dict.

    Keys in ``patch`` whose value is ``None`` are removed from the result
    (silently if absent). Keys with any other value overwrite the existing
    entry. Keys present in ``existing`` but absent from ``patch`` are
    preserved. ``patch == {}`` is a no-op that returns a fresh copy of
    ``existing``.

    Pure: returns a new dict; neither argument is mutated. The
    multi-writer guarantee in #290 depends on this being called inside a
    BEGIN IMMEDIATE transaction so the read-merge-write cycle is atomic.
    """
    merged = dict(existing)
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


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

    @traced("lithos.coordination.initialize")
    async def initialize(self) -> None:
        """Initialize database schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # One-time migration: move coordination.db from old root location to .lithos/
        old_path = self.config.storage.data_dir / "coordination.db"
        if old_path.exists() and not self.db_path.exists() and old_path != self.db_path:
            old_path.rename(self.db_path)
            logger.info(
                "coordination.db migrated: old_path=%s new_path=%s",
                old_path,
                self.db_path,
            )

        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await self._migrate_tasks_add_outcome(db)
            await self._migrate_tasks_ensure_resolved_at(db)
            await self._migrate_tasks_add_metadata(db)
            await db.commit()
        logger.info("coordination service initialized: db_path=%s", self.db_path)

    @staticmethod
    async def _migrate_tasks_add_outcome(db: aiosqlite.Connection) -> None:
        """Add outcome column to existing tasks tables (pre-outcome databases)."""
        cursor = await db.execute("PRAGMA table_info(tasks)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "outcome" not in columns:
            await db.execute("ALTER TABLE tasks ADD COLUMN outcome TEXT")
            logger.info("coordination.db migration applied: added tasks.outcome")

    @staticmethod
    async def _migrate_tasks_ensure_resolved_at(db: aiosqlite.Connection) -> None:
        """Ensure tasks.resolved_at exists, renaming legacy completed_at if present.

        Handles three legitimate pre-states for the ``tasks`` schema:

        * ``resolved_at`` already present — no-op (idempotent re-run, or fresh
          install where the CREATE TABLE statement supplied the column).
        * Only ``completed_at`` present — rename to ``resolved_at`` via
          ``ALTER TABLE ... RENAME COLUMN`` (the common upgrade path; data
          preserved row-for-row by SQLite).
        * Neither present — ancient pre-#178 database that never received the
          ``completed_at`` migration; add ``resolved_at`` as a NULL column.

        The defensive fourth state — both columns present — is logged loudly
        and left alone; ``resolved_at`` already exists so the migration is
        effectively complete.

        Robustness measures:

        * ``ALTER TABLE ... RENAME COLUMN`` requires SQLite >= 3.25.0 (Sept
          2018). Before renaming we assert the runtime SQLite supports it and
          raise with a clear upgrade message if it does not — daemon refuses to
          start rather than leaving a half-migrated DB.
        * Row count is captured before and after the ALTER and a mismatch
          raises ``RuntimeError``. SQLite's RENAME COLUMN does not move rows,
          but the explicit check protects against future migration evolution.
        """
        cursor = await db.execute("PRAGMA table_info(tasks)")
        columns = {row[1] for row in await cursor.fetchall()}

        if "resolved_at" in columns:
            if "completed_at" in columns:
                # Defensive: both columns present (only reachable if a
                # previous migration attempt partially succeeded, or if
                # external tooling added one of the columns out-of-band).
                # Read paths (get_task, list_tasks SQL filter, list_tasks
                # payload) look only at resolved_at, so rows whose timestamp
                # landed in completed_at would silently vanish from the
                # public surface. Backfill resolved_at from completed_at
                # where resolved_at IS NULL, then leave the orphan
                # completed_at column in place for forensic inspection.
                # We do not DROP completed_at — DROP COLUMN needs SQLite
                # >= 3.35 and operators may want to inspect the legacy
                # values manually.
                update_cursor = await db.execute(
                    "UPDATE tasks SET resolved_at = completed_at "
                    "WHERE resolved_at IS NULL AND completed_at IS NOT NULL"
                )
                logger.warning(
                    "coordination.db migration: both 'completed_at' and 'resolved_at' "
                    "columns present on tasks; backfilled resolved_at from completed_at "
                    "for %d row(s) and left completed_at in place for forensic "
                    "inspection.",
                    update_cursor.rowcount,
                )
            return

        cursor = await db.execute("SELECT COUNT(*) FROM tasks")
        row = await cursor.fetchone()
        row_count_before = row[0] if row else 0

        if "completed_at" in columns:
            if sqlite3.sqlite_version_info < (3, 25, 0):
                raise RuntimeError(
                    "coordination.db migration requires SQLite >= 3.25.0 to rename "
                    "the 'completed_at' column to 'resolved_at'. Current SQLite "
                    f"version is {sqlite3.sqlite_version}. Please upgrade SQLite "
                    "(e.g. by upgrading the host Python or container base image) "
                    "and restart."
                )
            await db.execute("ALTER TABLE tasks RENAME COLUMN completed_at TO resolved_at")
            logger.info(
                "coordination.db migration applied: renamed tasks.completed_at -> "
                "tasks.resolved_at (rows=%d)",
                row_count_before,
            )
        else:
            await db.execute("ALTER TABLE tasks ADD COLUMN resolved_at TIMESTAMP")
            logger.info(
                "coordination.db migration applied: added tasks.resolved_at (rows=%d)",
                row_count_before,
            )

        cursor = await db.execute("SELECT COUNT(*) FROM tasks")
        row = await cursor.fetchone()
        row_count_after = row[0] if row else 0
        if row_count_after != row_count_before:
            raise RuntimeError(
                "coordination.db migration row count mismatch on tasks: "
                f"before={row_count_before} after={row_count_after}. Aborting."
            )

    @staticmethod
    async def _migrate_tasks_add_metadata(db: aiosqlite.Connection) -> None:
        """Add metadata column to existing tasks tables."""
        cursor = await db.execute("PRAGMA table_info(tasks)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "metadata" not in columns:
            await db.execute("ALTER TABLE tasks ADD COLUMN metadata JSON")
            logger.info("coordination.db migration applied: added tasks.metadata")

    async def _get_db(self) -> aiosqlite.Connection:
        """Get database connection."""
        return await aiosqlite.connect(self.db_path)

    # ==================== Agent Operations ====================

    @traced("lithos.coordination.ensure_agent_known")
    async def ensure_agent_known(self, agent_id: str) -> None:
        """Ensure agent is registered, auto-registering if needed."""
        logger.debug("ensure_agent_known: agent_id=%s", agent_id)
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
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Create a new task.

        Args:
            title: Task title
            agent: Creating agent identifier
            description: Task description
            tags: Task tags
            metadata: Arbitrary JSON metadata dict (optional)

        Returns:
            Task ID
        """
        import json

        lithos_metrics.coordination_ops.add(1, {"op": "create_task"})
        await self.ensure_agent_known(agent)

        task_id = str(uuid.uuid4())
        tags_json = json.dumps(tags) if tags else None
        metadata_json = json.dumps(metadata) if metadata is not None else None
        now = _format_datetime(datetime.now(timezone.utc))

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO tasks (id, title, description, created_by, tags, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, title, description, agent, tags_json, now, metadata_json),
            )
            await db.commit()

        logger.info("Task created: task_id=%s agent=%s", task_id, agent)
        return task_id

    @traced("lithos.coordination.get_task")
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

            # outcome/resolved_at/metadata may be absent on legacy rows (pre-migration
            # reads should not be possible, but defend against it defensively).
            row_keys = row.keys()
            outcome = row["outcome"] if "outcome" in row_keys else None
            resolved_at_raw = row["resolved_at"] if "resolved_at" in row_keys else None

            task_metadata: dict[str, Any] = {}
            if "metadata" in row_keys:
                task_metadata = _decode_metadata(row["metadata"])

            return Task(
                id=row["id"],
                title=row["title"],
                description=row["description"],
                status=row["status"],
                created_by=row["created_by"],
                created_at=_parse_datetime(row["created_at"]),
                tags=tags,
                outcome=outcome,
                resolved_at=_parse_datetime(resolved_at_raw),
                metadata=task_metadata,
            )

    @traced("lithos.coordination.update_task")
    async def update_task(
        self,
        task_id: str,
        agent: str,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Update mutable task metadata.

        Only updates fields that are not None (partial update pattern).
        Only open tasks can be updated; completed or cancelled tasks are
        treated as not found (consistent with complete_task behaviour).

        ``metadata`` is applied as an additive per-key merge: keys with
        non-null values overwrite, keys whose value is ``None`` are deleted,
        keys not in the patch are preserved. ``metadata={}`` is a no-op.
        There is no wholesale-clear affordance (#290).

        Returns:
            True if task was found, is open, and was updated; False otherwise
        """
        import json

        lithos_metrics.coordination_ops.add(1, {"op": "update_task"})
        await self.ensure_agent_known(agent)

        non_metadata_sets: list[str] = []
        non_metadata_params: list[Any] = []

        if title is not None:
            non_metadata_sets.append("title = ?")
            non_metadata_params.append(title)
        if description is not None:
            non_metadata_sets.append("description = ?")
            non_metadata_params.append(description)
        if tags is not None:
            non_metadata_sets.append("tags = ?")
            non_metadata_params.append(json.dumps(tags))

        if metadata is None:
            return await self._update_task_fast(
                task_id, agent, non_metadata_sets, non_metadata_params
            )
        return await self._update_task_with_merge(
            task_id, agent, non_metadata_sets, non_metadata_params, metadata
        )

    async def _update_task_fast(
        self,
        task_id: str,
        agent: str,
        sets: list[str],
        params: list[Any],
    ) -> bool:
        """Update title/description/tags without touching metadata.

        Single UPDATE, no SELECT needed. When ``sets`` is empty the caller
        passed only no-op arguments — we still return True/False based on
        whether the task exists and is open, matching the contract of the
        merge path.
        """
        if not sets:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT id FROM tasks WHERE id = ? AND status = 'open'", (task_id,)
                )
                return await cursor.fetchone() is not None

        params_with_id = [*params, task_id]
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id = ? AND status = 'open'",
                params_with_id,
            )
            await db.commit()
            updated = cursor.rowcount > 0
            if updated:
                updated_fields = [clause.split(" = ")[0] for clause in sets]
                logger.info(
                    "Task updated: task_id=%s agent=%s fields=%s",
                    task_id,
                    agent,
                    updated_fields,
                    extra={"task_id": task_id, "agent": agent, "fields": updated_fields},
                )
            return updated

    async def _update_task_with_merge(
        self,
        task_id: str,
        agent: str,
        non_metadata_sets: list[str],
        non_metadata_params: list[Any],
        metadata_patch: dict[str, Any],
    ) -> bool:
        """Read-merge-write the metadata column inside BEGIN IMMEDIATE.

        BEGIN IMMEDIATE acquires the database-level write lock at the start
        of the transaction, so two concurrent callers writing different
        keys cannot both pass the SELECT and then race on the UPDATE — the
        second caller blocks until the first commits, then reads the merged
        state. This is the property the #290 multi-writer guarantee relies on.
        """
        import json

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT metadata FROM tasks WHERE id = ? AND status = 'open'",
                    (task_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    await db.execute("ROLLBACK")
                    return False

                existing = _decode_metadata(row[0])
                merged = _merge_metadata(existing, metadata_patch)
                merged_json = json.dumps(merged)

                sets = [*non_metadata_sets, "metadata = ?"]
                params = [*non_metadata_params, merged_json, task_id]
                await db.execute(
                    f"UPDATE tasks SET {', '.join(sets)} WHERE id = ? AND status = 'open'",
                    params,
                )
                await db.commit()
            except Exception:
                with contextlib.suppress(Exception):
                    await db.execute("ROLLBACK")
                raise

        updated_fields = [clause.split(" = ")[0] for clause in non_metadata_sets]
        updated_fields.append("metadata")
        logger.info(
            "Task updated: task_id=%s agent=%s fields=%s",
            task_id,
            agent,
            updated_fields,
            extra={"task_id": task_id, "agent": agent, "fields": updated_fields},
        )
        return True

    @traced("lithos.coordination.complete_task")
    async def complete_task(
        self,
        task_id: str,
        agent: str,
        outcome: str | None = None,
    ) -> bool:
        """Mark task as completed and release all claims.

        Args:
            task_id: Task ID to complete.
            agent: Agent completing the task.
            outcome: Optional free-text completion summary persisted alongside
                the task. Downstream consolidation (LCMA enrich) can use this
                as the ``outcome`` slot of the frame extracted from the task.

        Returns:
            True if task was completed
        """
        lithos_metrics.coordination_ops.add(1, {"op": "complete"})
        await self.ensure_agent_known(agent)

        now = _format_datetime(datetime.now(timezone.utc))

        async with aiosqlite.connect(self.db_path) as db:
            # Update task status, outcome, and resolved_at in a single statement
            cursor = await db.execute(
                """
                UPDATE tasks
                   SET status = 'completed',
                       outcome = ?,
                       resolved_at = ?
                 WHERE id = ? AND status = 'open'
                """,
                (outcome, now, task_id),
            )
            if cursor.rowcount == 0:
                return False

            # Release all claims
            await db.execute(
                "DELETE FROM claims WHERE task_id = ?",
                (task_id,),
            )

            await db.commit()
            logger.info(
                "Task completed: task_id=%s agent=%s outcome_len=%d",
                task_id,
                agent,
                len(outcome) if outcome else 0,
                extra={
                    "task_id": task_id,
                    "agent": agent,
                    "outcome_provided": outcome is not None,
                    "outcome_len": len(outcome) if outcome else 0,
                },
            )
            return True

    @traced("lithos.coordination.cancel_task")
    async def cancel_task(self, task_id: str, agent: str, reason: str | None = None) -> bool:
        """Mark task as cancelled and release all claims.

        Returns:
            True if task was cancelled
        """
        lithos_metrics.coordination_ops.add(1, {"op": "cancel"})
        await self.ensure_agent_known(agent)

        now = _format_datetime(datetime.now(timezone.utc))

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE tasks
                   SET status = 'cancelled',
                       resolved_at = ?
                 WHERE id = ? AND status = 'open'
                """,
                (now, task_id),
            )
            if cursor.rowcount == 0:
                return False

            await db.execute(
                "DELETE FROM claims WHERE task_id = ?",
                (task_id,),
            )

            await db.commit()
            logger.info(
                "Task cancelled: task_id=%s agent=%s reason=%s",
                task_id,
                agent,
                reason,
                extra={"task_id": task_id, "agent": agent, "reason": reason},
            )
            return True

    async def list_tasks(
        self,
        agent: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        since: str | None = None,
        resolved_since: str | None = None,
        with_claims: bool = False,
    ) -> list[dict[str, Any]]:
        """List tasks with optional filters.

        Args:
            agent: Filter by created_by agent
            status: Filter by status (open/completed/cancelled), or None for all
            tags: Filter by tags (task must have all specified tags)
            since: Filter by created_at >= this ISO datetime string
            resolved_since: Filter by resolved_at >= this ISO datetime string.
                ``resolved_at`` is set on both terminal transitions (complete
                and cancel). Rows whose ``resolved_at`` is NULL — open tasks,
                and historical cancellations from before the column was
                populated on cancel — are excluded automatically by the
                SQL ``>=`` comparison.
            with_claims: When True, include each task's active (non-expired)
                claims inline as a ``claims`` array. Defaults to False to
                preserve the lightweight payload for callers that don't need
                them.

        Returns:
            List of task dicts with id, title, description, status, created_by,
            created_at, resolved_at, tags, metadata, outcome, and (when
            with_claims) claims.
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

        if resolved_since:
            query += " AND resolved_at >= ?"
            params.append(resolved_since)

        query += " ORDER BY created_at DESC"

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()

            results: list[dict[str, Any]] = []
            row_keys: list[str] | None = None
            for row in rows:
                if row_keys is None:
                    row_keys = list(row.keys())

                task_tags: list[str] = []
                if row["tags"]:
                    with contextlib.suppress(json.JSONDecodeError):
                        task_tags = json.loads(row["tags"])

                # Filter by tags: task must contain all requested tags
                if tags and not all(t in task_tags for t in tags):
                    continue

                # Route metadata decode through _decode_metadata for parity
                # with get_task / get_task_status. Raw json.loads accepted
                # legacy/corrupt rows whose JSON parses to non-objects
                # (`null`, arrays, scalars), causing list_tasks to return a
                # non-dict metadata payload while the other surfaces regularised
                # it back to {}.
                task_metadata: dict[str, Any] = {}
                if "metadata" in row_keys:
                    task_metadata = _decode_metadata(row["metadata"])

                resolved_at = (
                    row["resolved_at"]
                    if row_keys is not None and "resolved_at" in row_keys
                    else None
                )
                outcome = row["outcome"] if row_keys is not None and "outcome" in row_keys else None
                results.append(
                    {
                        "id": row["id"],
                        "title": row["title"],
                        "description": row["description"],
                        "status": row["status"],
                        "created_by": row["created_by"],
                        "created_at": row["created_at"],
                        "resolved_at": resolved_at,
                        "tags": task_tags,
                        "metadata": task_metadata,
                        "outcome": outcome,
                    }
                )

            if with_claims and results:
                claims_by_task = await self._fetch_active_claims_for(db, [r["id"] for r in results])
                for task in results:
                    task["claims"] = claims_by_task.get(task["id"], [])

            return results

    async def _fetch_active_claims_for(
        self,
        db: aiosqlite.Connection,
        task_ids: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        """Fetch active (non-expired) claims for a batch of task IDs.

        Issues a single SQL query rather than one per task. Returns a dict
        mapping task_id -> list of claim dicts in the same shape the
        ``lithos_task_status`` MCP tool emits.
        """
        if not task_ids:
            return {}

        now = _format_datetime(datetime.now(timezone.utc))
        placeholders = ",".join("?" for _ in task_ids)
        cursor = await db.execute(
            f"""
            SELECT task_id, agent, aspect, expires_at
            FROM claims
            WHERE task_id IN ({placeholders}) AND expires_at > ?
            """,
            (*task_ids, now),
        )
        rows = await cursor.fetchall()

        grouped: dict[str, list[dict[str, Any]]] = {tid: [] for tid in task_ids}
        for row in rows:
            expires_dt = _parse_datetime(row["expires_at"]) or datetime.now(timezone.utc)
            grouped[row["task_id"]].append(
                {
                    "agent": row["agent"],
                    "aspect": row["aspect"],
                    "expires_at": expires_dt.isoformat(),
                }
            )
        return grouped

    @traced("lithos.coordination.get_task_status")
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
        import json

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
            task_row_keys: list[str] | None = None

            for task in tasks:
                if task_row_keys is None:
                    task_row_keys = list(task.keys())

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

                task_metadata: dict[str, Any] = {}
                if "metadata" in task_row_keys and task["metadata"]:
                    task_metadata = _decode_metadata(task["metadata"])

                task_tags: list[str] = []
                if task["tags"]:
                    with contextlib.suppress(json.JSONDecodeError):
                        task_tags = json.loads(task["tags"])

                outcome = task["outcome"] if "outcome" in task_row_keys else None
                resolved_at_raw = task["resolved_at"] if "resolved_at" in task_row_keys else None

                result.append(
                    TaskStatus(
                        id=task["id"],
                        title=task["title"],
                        status=task["status"],
                        claims=claims,
                        metadata=task_metadata,
                        description=task["description"],
                        created_by=task["created_by"],
                        created_at=_parse_datetime(task["created_at"]),
                        tags=task_tags,
                        outcome=outcome,
                        resolved_at=_parse_datetime(resolved_at_raw),
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
                logger.info(
                    "Claim acquired: task_id=%s agent=%s aspect=%s",
                    task_id,
                    agent,
                    aspect,
                )
                return True, expires_at
            logger.warning(
                "Claim conflict: task_id=%s aspect=%s requested_by=%s",
                task_id,
                aspect,
                agent,
            )
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
                logger.warning(
                    "Claim renewal failed — no active claim: task_id=%s aspect=%s agent=%s",
                    task_id,
                    aspect,
                    agent,
                )
                return False, None  # No active claim

            if row[0] != agent:
                logger.warning(
                    "Expired claim access attempt: task_id=%s aspect=%s claimant=%s attempted_by=%s",
                    task_id,
                    aspect,
                    row[0],
                    agent,
                )
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
            logger.debug("Claim renewed: task_id=%s aspect=%s agent=%s", task_id, aspect, agent)
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
            released = cursor.rowcount > 0
            if released:
                logger.info(
                    "Claim released: task_id=%s aspect=%s agent=%s",
                    task_id,
                    aspect,
                    agent,
                    extra={"task_id": task_id, "aspect": aspect, "agent": agent},
                )
            else:
                logger.debug(
                    "Claim release no-op (not held): task_id=%s aspect=%s agent=%s",
                    task_id,
                    aspect,
                    agent,
                )
            return released

    # ==================== Finding Operations ====================

    @traced("lithos.coordination.post_finding")
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

        logger.info(
            "Finding posted: task_id=%s agent=%s finding_id=%s summary=%.80s",
            task_id,
            agent,
            finding_id,
            summary,
        )
        return finding_id

    @traced("lithos.coordination.list_findings")
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

    # ==================== Audit Log ====================

    async def log_access(
        self,
        doc_id: str,
        operation: Literal["read", "search_result"],
        agent_id: str = "unknown",
    ) -> None:
        """Append a read-access entry to the audit log.

        Failures are swallowed — audit logging must never degrade the hot path.

        Args:
            doc_id: The document that was accessed.
            operation: ``"read"`` (direct lithos_read call) or
                       ``"search_result"`` (document returned in search results).
            agent_id: The agent that triggered the access (default: ``"unknown"``).
        """
        now = _format_datetime(datetime.now(timezone.utc))
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO access_log (agent_id, doc_id, operation, timestamp) VALUES (?, ?, ?, ?)",
                    (agent_id, doc_id, operation, now),
                )
                await db.commit()
        except Exception:
            logger.debug("audit log_access failed (non-fatal)", exc_info=True)

    async def log_access_batch(
        self,
        doc_ids: list[str],
        operation: Literal["read", "search_result"],
        agent_id: str = "unknown",
    ) -> None:
        """Append multiple read-access entries to the audit log in a single write.

        Prefer this over calling :meth:`log_access` in a loop for bulk operations
        (e.g. search results) to avoid opening N concurrent SQLite connections.

        Failures are swallowed — audit logging must never degrade the hot path.

        Args:
            doc_ids: Documents that were accessed.
            operation: ``"read"`` or ``"search_result"``.
            agent_id: The agent that triggered the access (default: ``"unknown"``).
                      Note: ``agent_id`` is self-reported and spoofable; the audit
                      log is advisory-only and should not be used for access control.
        """
        if not doc_ids:
            return
        now = _format_datetime(datetime.now(timezone.utc))
        rows = [(agent_id, doc_id, operation, now) for doc_id in doc_ids]
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.executemany(
                    "INSERT INTO access_log (agent_id, doc_id, operation, timestamp) VALUES (?, ?, ?, ?)",
                    rows,
                )
                await db.commit()
        except Exception:
            logger.debug("audit log_access_batch failed (non-fatal)", exc_info=True)

    async def get_audit_log(
        self,
        agent_id: str | None = None,
        after: str | None = None,
        limit: int = 100,
        doc_id: str | None = None,
    ) -> list[AccessLogEntry]:
        """Query the read-access audit log.

        Args:
            agent_id: Filter to entries from this agent (optional).
            after: ISO-8601 timestamp; only return entries after this time (optional).
            limit: Maximum number of entries to return (default: 100, max: 1000).
            doc_id: Filter to entries for this document (optional).

        Returns:
            List of :class:`AccessLogEntry` objects, most-recent first.
        """
        limit = max(1, min(1000, limit))
        conditions: list[str] = []
        params: list[str | int] = []

        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if after:
            conditions.append("timestamp > ?")
            params.append(after)
        if doc_id:
            conditions.append("doc_id = ?")
            params.append(doc_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    f"SELECT id, agent_id, doc_id, operation, timestamp "
                    f"FROM access_log {where} ORDER BY timestamp DESC LIMIT ?",
                    params,
                )
                rows = await cursor.fetchall()
        except Exception:
            logger.error("get_audit_log failed (non-fatal)", exc_info=True)
            return []

        return [
            AccessLogEntry(
                id=row[0],
                agent_id=row[1],
                doc_id=row[2],
                operation=row[3],
                timestamp=_parse_datetime(row[4]),
            )
            for row in rows
        ]

    async def get_retrieval_count(self, doc_id: str) -> int:
        """Return how many times a document has been directly read (operation='read').

        Args:
            doc_id: Document ID to count reads for.

        Returns:
            Number of ``read`` entries in the audit log for this document.
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM access_log WHERE doc_id = ? AND operation = 'read'",
                    (doc_id,),
                )
                row = await cursor.fetchone()
                return row[0] if row else 0
        except Exception:
            logger.debug("get_retrieval_count failed (non-fatal)", exc_info=True)
            return 0

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

            # Count expired claims (still on disk, not yet cleaned up)
            cursor = await db.execute(
                "SELECT COUNT(*) FROM claims WHERE expires_at <= ?",
                (now,),
            )
            row = await cursor.fetchone()
            expired_claims = row[0] if row else 0

            return {
                "agents": agents,
                "active_tasks": active_tasks,
                "open_claims": open_claims,
                "expired_claims": expired_claims,
            }
