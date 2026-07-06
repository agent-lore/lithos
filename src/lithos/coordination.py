"""Coordination service - SQLite-based tasks, claims, agents, findings."""

import contextlib
import logging
import sqlite3
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from lithos._merge import merge_metadata
from lithos.config import LithosConfig, get_config
from lithos.errors import CoordinationError
from lithos.telemetry import lithos_metrics, traced

logger = logging.getLogger(__name__)

# Edge types accepted on write. The set grows per delivery phase: an edge type
# is only accepted once the phase implementing its readiness semantics has
# shipped, so agents can never write a blocking edge whose meaning is not yet
# implemented. Phase 1 ships blocks/parent_child/discovered_from. Phase 3 adds
# ``waits_on_gate``. Deferred types (caused_by, validates, relates_to,
# duplicate_of, superseded_by) are added only when something consumes them.
ACCEPTED_EDGE_TYPES: frozenset[str] = frozenset(
    {"blocks", "parent_child", "discovered_from", "waits_on_gate"}
)

# Edge types resolved by predecessor ``completed`` status. A task is not ready
# while it has an incoming ``blocks`` edge whose predecessor is not ``completed``.
BLOCKING_EDGE_TYPES: frozenset[str] = frozenset({"blocks"})

# Gate edges. A ``waits_on_gate`` edge blocks the dependent until the gate task
# (the ``from`` end) is resolved — see _unsatisfied_blocker_sql for the rule.
GATE_EDGE_TYPES: frozenset[str] = frozenset({"waits_on_gate"})

# The full "X waits on Y" dependency graph (blocks + gates). Readiness and cycle
# detection operate over this combined set so mixed cycles are rejected.
DEPENDENCY_EDGE_TYPES: frozenset[str] = BLOCKING_EDGE_TYPES | GATE_EDGE_TYPES

# Hierarchy edges. Purely structural (never block readiness), but must stay
# acyclic — a task cannot be its own ancestor — so cycles are rejected on write.
HIERARCHY_EDGE_TYPES: frozenset[str] = frozenset({"parent_child"})

# Task types accepted on write this phase (see ACCEPTED_EDGE_TYPES rationale).
# The column defaults to 'task' and physically holds any string; only the
# write-validation set is phase-gated.
ACCEPTED_TASK_TYPES: frozenset[str] = frozenset({"task", "epic", "gate"})

# Valid ``gate_type`` values for a ``task_type='gate'`` task.
GATE_TYPES: frozenset[str] = frozenset({"human", "timer", "ci", "pr", "external_task"})

# Task types that are never directly workable units and so are excluded from the
# ready frontier. ``epic`` is a roll-up container (you execute its children, not
# the epic); ``gate`` is an external wait.
NON_WORKABLE_TASK_TYPES: tuple[str, ...] = ("gate", "epic")

# Scheduling-convention metadata keys that ``lithos_task_spawn`` copies from the
# source task when ``inherit_context`` is set, so follow-on work keeps its place
# in the schedule. Forbidden keys (FORBIDDEN_METADATA_KEYS) are never inherited.
INHERITABLE_CONTEXT_KEYS: tuple[str, ...] = ("priority", "parallelizable", "phase")

# metadata keys whose scheduling meaning is now owned by task_edges. Writing them
# is rejected so stale, scheduler-invisible dependency state cannot be recreated
# through the additive metadata write path (see SPECIFICATION migration notes).
FORBIDDEN_METADATA_KEYS: tuple[str, ...] = ("depends_on", "blocked_on")


def _reject_scheduling_metadata(metadata: dict[str, Any] | None) -> None:
    """Raise if ``metadata`` carries a now-forbidden scheduling key.

    Rejects any presence of ``depends_on``/``blocked_on`` (including a ``None``
    delete), because even a delete implies the caller still treats metadata as
    the source of dependency truth. Dependencies are first-class edges now.
    """
    if not metadata:
        return
    present = [k for k in FORBIDDEN_METADATA_KEYS if k in metadata]
    if present:
        raise CoordinationError(
            "invalid_metadata_key",
            f"metadata key(s) {present} are no longer accepted: task dependencies are "
            "first-class task edges. Use depends_on on lithos_task_create, or "
            "lithos_task_edge_upsert.",
        )


def _validate_gate_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Validate + normalize the metadata of a ``task_type='gate'`` task.

    Requires a valid ``gate_type``; ``timer`` gates require a parseable
    ``ready_at``, which is rewritten to a canonical UTC second-precision ISO
    string (a naive value is interpreted as UTC) so the ready/blocked timer
    comparison is one consistent lexicographic check. Returns a new metadata dict
    (the input is not mutated). Raises CoordinationError(invalid_input) on any
    violation.
    """
    md = dict(metadata or {})
    gate_type = md.get("gate_type")
    if gate_type not in GATE_TYPES:
        raise CoordinationError(
            "invalid_input",
            f"a gate task requires metadata.gate_type in {sorted(GATE_TYPES)}, got {gate_type!r}.",
        )
    if gate_type == "timer":
        raw = md.get("ready_at")
        parsed = _parse_datetime(raw) if raw is not None else None
        if parsed is None:
            raise CoordinationError(
                "invalid_input",
                f"a 'timer' gate requires a parseable metadata.ready_at (ISO datetime), got {raw!r}.",
            )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        md["ready_at"] = parsed.astimezone(UTC).replace(microsecond=0).isoformat()
    return md


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
    task_type TEXT NOT NULL DEFAULT 'task',
    created_by TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    tags JSON,
    outcome TEXT,
    resolved_at TIMESTAMP,
    metadata JSON
);

-- Typed task-graph edges (ordering, hierarchy, provenance)
CREATE TABLE IF NOT EXISTS task_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_task_id TEXT NOT NULL,
    to_task_id TEXT NOT NULL,
    type TEXT NOT NULL,
    metadata JSON,
    created_by TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (from_task_id) REFERENCES tasks(id),
    FOREIGN KEY (to_task_id) REFERENCES tasks(id),
    UNIQUE(from_task_id, to_task_id, type)
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
-- Ready/blocked queries join task_edges against open tasks; both directions
-- must stay index-driven (sub-linear). Cycle detection also traverses these.
CREATE INDEX IF NOT EXISTS idx_task_edges_from ON task_edges(from_task_id, type);
CREATE INDEX IF NOT EXISTS idx_task_edges_to ON task_edges(to_task_id, type);
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
    task_type: str = "task"
    created_by: str = ""
    created_at: datetime | None = None
    tags: list[str] = field(default_factory=list)
    outcome: str | None = None
    resolved_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskEdge:
    """A typed relation between two tasks in the task graph."""

    from_task_id: str
    to_task_id: str
    type: str
    created_by: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


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
        return datetime.now(UTC) > self.expires_at


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
    task_type: str = "task"
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


def _json_path_for_key(key: str) -> str:
    """Build a SQLite JSON path addressing a top-level metadata ``key`` (#306).

    Always uses the quoted-label form ``$."key"`` (valid for keys with dots,
    spaces, etc.), escaping embedded backslashes and quotes. The result is
    passed to ``json_extract``/``json_each`` as a *bound parameter*, so it
    cannot inject SQL — this only ensures the path addresses the right key.
    """
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'$."{escaped}"'


def _json_type_label(value: object) -> str:
    """Map a Python scalar to the ``json_type()`` label SQLite reports (#306).

    Used to make ``metadata_match`` type-sensitive: SQLite stores JSON booleans
    as 1/0, so without this a query value of ``1`` would match a stored ``true``.
    ``bool`` is checked before ``int`` because ``bool`` is an ``int`` subclass.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "real"
    return "text"


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


def _task_row_to_dict(row: Any) -> dict[str, Any]:
    """Build the standard task payload dict from a ``tasks`` row.

    Shared by ``list_tasks``/``list_ready``/``list_blocked`` so every surface
    emits the same shape, including ``task_type``. ``created_at``/``resolved_at``
    are returned as the raw stored strings (callers that need parsed datetimes
    use the dataclass surfaces). Columns absent on legacy rows degrade to safe
    defaults.
    """
    import json

    row_keys = row.keys()
    tags: list[str] = []
    if row["tags"]:
        with contextlib.suppress(json.JSONDecodeError):
            tags = json.loads(row["tags"])
    metadata = _decode_metadata(row["metadata"]) if "metadata" in row_keys else {}
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "status": row["status"],
        "task_type": row["task_type"] if "task_type" in row_keys else "task",
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"] if "resolved_at" in row_keys else None,
        "tags": tags,
        "metadata": metadata,
        "outcome": row["outcome"] if "outcome" in row_keys else None,
    }


def _metadata_match_clause(
    metadata_match: dict[str, Any] | None,
    column: str = "metadata",
) -> tuple[str, list[Any]]:
    """Build a type-sensitive ``metadata_match`` SQL fragment + bound params (#306).

    For each ``key: q`` the task matches when its stored value equals ``q`` or is
    an element of a JSON *array* at that key. ``column`` lets callers qualify the
    column (e.g. ``t.metadata``) when the query joins multiple tables. ``column``
    is an internal constant, never user input, so it cannot inject SQL; the JSON
    path and value are bound parameters.
    """
    if not metadata_match:
        return "", []
    clause = ""
    params: list[Any] = []
    for key, value in metadata_match.items():
        path = _json_path_for_key(key)
        jtype = _json_type_label(value)
        clause += (
            f" AND ( (json_type({column}, ?) = ? AND json_extract({column}, ?) = ?)"
            f" OR (json_type({column}, ?) = 'array' AND EXISTS"
            f" (SELECT 1 FROM json_each({column}, ?) WHERE type = ? AND value = ?)) )"
        )
        params.extend([path, jtype, path, value, path, path, jtype, value])
    return clause, params


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
            await self._migrate_tasks_add_task_type(db)
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

    @staticmethod
    async def _migrate_tasks_add_task_type(db: aiosqlite.Connection) -> None:
        """Add tasks.task_type and run the one-time metadata->edges backfill.

        The backfill is tied to the column-addition branch so it runs exactly
        once with no separate version table: an old DB lacks the column, gets it
        added, and is backfilled in the same migration; a fresh DB already has
        the column (from SCHEMA) and has no legacy dependency metadata, so both
        steps are skipped. ``initialize`` commits all migrations together, so the
        column add and the backfill are atomic — a mid-migration crash redoes
        both on the next start rather than leaving the column without its edges.
        """
        cursor = await db.execute("PRAGMA table_info(tasks)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "task_type" in columns:
            return
        await db.execute("ALTER TABLE tasks ADD COLUMN task_type TEXT NOT NULL DEFAULT 'task'")
        backfilled = await CoordinationService._backfill_task_edges_from_metadata(db)
        logger.info(
            "coordination.db migration applied: added tasks.task_type and backfilled "
            "%d blocks edge(s) from task metadata",
            backfilled,
        )

    @staticmethod
    async def _backfill_task_edges_from_metadata(db: aiosqlite.Connection) -> int:
        """One-time backfill of ``blocks`` edges from legacy dependency metadata.

        Scans ``open`` tasks, reads ``metadata.depends_on`` / ``metadata.blocked_on``
        (each a task id or list of ids), and creates a canonical ``blocks`` edge
        ``predecessor -> task`` for every reference to an existing task. References
        to nonexistent task ids are logged and skipped. Uses ``INSERT OR IGNORE``
        against the ``UNIQUE(from,to,type)`` constraint so it is idempotent.

        Edges are retained even when they form a cycle (never silently dropped):
        cycle members are excluded from ``ready`` automatically (mutual open
        blockers) and surfaced as ``cycle`` blockers by ``list_blocked``.
        """
        import json

        cursor = await db.execute("SELECT id FROM tasks")
        existing_ids = {row[0] for row in await cursor.fetchall()}

        cursor = await db.execute("SELECT id, metadata FROM tasks WHERE status = 'open'")
        rows = await cursor.fetchall()
        now = _format_datetime(datetime.now(UTC))
        created = 0
        for task_id, metadata_raw in rows:
            md = _decode_metadata(metadata_raw)
            for key in FORBIDDEN_METADATA_KEYS:
                preds = md.get(key)
                if preds is None:
                    continue
                pred_list = preds if isinstance(preds, list) else [preds]
                for pred in pred_list:
                    if not isinstance(pred, str) or pred == task_id:
                        continue
                    if pred not in existing_ids:
                        logger.warning(
                            "coordination.db backfill: task %s metadata.%s references "
                            "nonexistent task %s; skipping",
                            task_id,
                            key,
                            pred,
                        )
                        continue
                    edge_metadata = json.dumps({"migrated_from": f"metadata.{key}"})
                    edge_cursor = await db.execute(
                        """
                        INSERT OR IGNORE INTO task_edges
                            (from_task_id, to_task_id, type, metadata, created_by, created_at)
                        VALUES (?, ?, 'blocks', ?, 'migration', ?)
                        """,
                        (pred, task_id, edge_metadata, now),
                    )
                    created += edge_cursor.rowcount
        return created

    async def _get_db(self) -> aiosqlite.Connection:
        """Get database connection."""
        return await aiosqlite.connect(self.db_path)

    # ==================== Agent Operations ====================

    @traced("lithos.coordination.ensure_agent_known")
    async def ensure_agent_known(self, agent_id: str) -> None:
        """Ensure agent is registered, auto-registering if needed."""
        logger.debug("ensure_agent_known: agent_id=%s", agent_id)
        now = _format_datetime(datetime.now(UTC))
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

        now = _format_datetime(datetime.now(UTC))
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
        task_type: str = "task",
        depends_on: list[str] | None = None,
        parent_task_id: str | None = None,
    ) -> str:
        """Create a new task.

        Args:
            title: Task title
            agent: Creating agent identifier
            description: Task description
            tags: Task tags
            metadata: Arbitrary JSON metadata dict (optional). Must not contain
                ``depends_on``/``blocked_on`` — dependencies are task edges now.
            task_type: First-class task type (``task``, ``epic`` or ``gate``). A
                ``gate`` requires ``metadata.gate_type`` (human/timer/ci/pr/
                external_task); a ``timer`` gate also requires a parseable
                ``metadata.ready_at``.
            depends_on: Predecessor task ids. Each creates a ``blocks`` edge
                ``predecessor -> this task``. Predecessors must already exist.
            parent_task_id: Optional parent. Creates a ``parent_child`` edge
                ``parent -> this task``. The parent must already exist; it may be
                any task type (it need not be an ``epic``).

        Returns:
            Task ID

        Raises:
            CoordinationError: invalid task_type, forbidden metadata key, invalid
                gate metadata, or a ``depends_on``/``parent_task_id`` reference to
                a nonexistent task.
        """
        import json

        _reject_scheduling_metadata(metadata)
        if task_type not in ACCEPTED_TASK_TYPES:
            raise CoordinationError(
                "invalid_task_type",
                f"task_type '{task_type}' is not accepted in this phase "
                f"(accepted: {sorted(ACCEPTED_TASK_TYPES)}).",
            )
        if task_type == "gate":
            metadata = _validate_gate_metadata(metadata)

        lithos_metrics.coordination_ops.add(1, {"op": "create_task"})
        await self.ensure_agent_known(agent)

        task_id = str(uuid.uuid4())
        tags_json = json.dumps(tags) if tags else None
        metadata_json = json.dumps(metadata) if metadata is not None else None
        now = _format_datetime(datetime.now(UTC))
        # Dedupe and drop a self-reference; a brand-new task has no outgoing
        # edges, so depends_on/parent_task_id can never form a cycle (nothing
        # depends on or descends from it yet).
        predecessors = [p for p in dict.fromkeys(depends_on or []) if p != task_id]
        parent = parent_task_id if parent_task_id != task_id else None

        async with aiosqlite.connect(self.db_path) as db:
            referenced = {*predecessors, *([parent] if parent else [])}
            if referenced:
                placeholders = ",".join("?" for _ in referenced)
                cursor = await db.execute(
                    f"SELECT id FROM tasks WHERE id IN ({placeholders})",
                    tuple(referenced),
                )
                found = {row[0] for row in await cursor.fetchall()}
                missing_deps = [p for p in predecessors if p not in found]
                if missing_deps:
                    raise CoordinationError(
                        "task_not_found",
                        f"depends_on references nonexistent task(s): {missing_deps}",
                    )
                if parent and parent not in found:
                    raise CoordinationError(
                        "task_not_found",
                        f"parent_task_id references nonexistent task: {parent}",
                    )

            await db.execute(
                """
                INSERT INTO tasks
                    (id, title, description, status, task_type, created_by, tags,
                     created_at, metadata)
                VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)
                """,
                (task_id, title, description, task_type, agent, tags_json, now, metadata_json),
            )
            for pred in predecessors:
                await db.execute(
                    """
                    INSERT OR IGNORE INTO task_edges
                        (from_task_id, to_task_id, type, created_by, created_at)
                    VALUES (?, ?, 'blocks', ?, ?)
                    """,
                    (pred, task_id, agent, now),
                )
            if parent:
                await db.execute(
                    """
                    INSERT OR IGNORE INTO task_edges
                        (from_task_id, to_task_id, type, created_by, created_at)
                    VALUES (?, ?, 'parent_child', ?, ?)
                    """,
                    (parent, task_id, agent, now),
                )
            await db.commit()

        logger.info(
            "Task created: task_id=%s agent=%s task_type=%s depends_on=%d parent=%s",
            task_id,
            agent,
            task_type,
            len(predecessors),
            parent,
        )
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
                task_type=row["task_type"] if "task_type" in row_keys else "task",
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
        Terminal tasks (completed/cancelled) are updatable too (#303) — useful for
        annotating an archived task (e.g. a metadata snapshot) without reviving it;
        use ``lithos_task_reopen`` to bring a task back to active work.

        ``metadata`` is applied as an additive per-key merge: keys with
        non-null values overwrite, keys whose value is ``None`` are deleted,
        keys not in the patch are preserved. ``metadata={}`` is a no-op.
        There is no wholesale-clear affordance (#290).

        Returns:
            True if task was found and updated; False if no such task exists

        Raises:
            CoordinationError: ``metadata`` contains a forbidden scheduling key
                (``depends_on``/``blocked_on``).
        """
        import json

        _reject_scheduling_metadata(metadata)
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
        whether the task exists, matching the contract of the merge path.
        Terminal tasks (completed/cancelled) are updatable too (#303).
        """
        if not sets:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("SELECT id FROM tasks WHERE id = ?", (task_id,))
                return await cursor.fetchone() is not None

        params_with_id = [*params, task_id]
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?",
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
                    "SELECT task_type, metadata FROM tasks WHERE id = ?",
                    (task_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    await db.execute("ROLLBACK")
                    return False

                existing = _decode_metadata(row[1])
                merged = merge_metadata(existing, metadata_patch)
                # A gate's metadata must stay valid: revalidate (and re-normalize a
                # timer ready_at) so a patch like {"gate_type": None} can't strip
                # the gate invariants and spuriously ready its waiters.
                if row[0] == "gate":
                    merged = _validate_gate_metadata(merged)
                merged_json = json.dumps(merged)

                sets = [*non_metadata_sets, "metadata = ?"]
                params = [*non_metadata_params, merged_json, task_id]
                await db.execute(
                    f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?",
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

        now = _format_datetime(datetime.now(UTC))

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

        now = _format_datetime(datetime.now(UTC))

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

    @traced("lithos.coordination.reopen_task")
    async def reopen_task(self, task_id: str, agent: str) -> tuple[str, str | None]:
        """Move a terminal task back to ``open`` (the inverse of complete/cancel).

        Clears ``resolved_at`` and ``outcome`` — a reopened task is no longer
        resolved. Claims were already released on complete/cancel, so there is
        nothing to restore. Returns ``(prior_status, prior_outcome)`` so the
        caller can record the reopen in an event/finding.

        Raises:
            CoordinationError: ``task_not_found`` (unknown id) or
                ``task_not_resolved`` (the task is already ``open``).
        """
        lithos_metrics.coordination_ops.add(1, {"op": "reopen"})
        await self.ensure_agent_known(agent)

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT status, outcome FROM tasks WHERE id = ?",
                (task_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise CoordinationError("task_not_found", f"Task '{task_id}' not found.")
            prior_status, prior_outcome = row[0], row[1]
            if prior_status == "open":
                raise CoordinationError(
                    "task_not_resolved", f"Task '{task_id}' is already open; nothing to reopen."
                )
            await db.execute(
                "UPDATE tasks SET status = 'open', resolved_at = NULL, outcome = NULL WHERE id = ?",
                (task_id,),
            )
            await db.commit()

        logger.info(
            "Task reopened: task_id=%s agent=%s prior_status=%s",
            task_id,
            agent,
            prior_status,
            extra={"task_id": task_id, "agent": agent, "prior_status": prior_status},
        )
        return prior_status, prior_outcome

    @traced("lithos.coordination.newly_reblocked_by")
    async def newly_reblocked_by(self, task_id: str, prior_status: str) -> list[str]:
        """Return ids of open dependents this reopen just put back under a block.

        The inverse of :meth:`newly_unblocked_by`. Only a ``completed``-task
        reopen newly blocks anyone: a ``cancelled``-task reopen *un-strands* its
        dependents (``blocker_unsatisfiable`` -> waiting) without newly blocking
        them, so it returns ``[]``. For a completed reopen, a dependent is
        reported iff its *only* current blocker is the reopened task (it was ready
        before and is now blocked solely by this), so the report stays honest and
        does not include dependents that were already blocked by something else.
        """
        if prior_status != "completed":
            return []
        dependency = tuple(DEPENDENCY_EDGE_TYPES)
        placeholders = ",".join("?" for _ in dependency)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # Restrict to *open* dependents: a terminal dependent is not active work
            # and was never "ready", so reporting it as reblocked would mislead an
            # orchestrator (the reopened task can be the sole blocker of a long-since
            # completed dependent — _compute_blockers ignores the dependent's status).
            cursor = await db.execute(
                f"SELECT DISTINCT e.to_task_id FROM task_edges e "
                f"JOIN tasks t ON t.id = e.to_task_id "
                f"WHERE e.from_task_id = ? AND e.type IN ({placeholders}) "
                f"AND t.status = 'open'",
                (task_id, *dependency),
            )
            candidates = [row[0] for row in await cursor.fetchall()]
            reblocked: list[str] = []
            for cand in candidates:
                blockers = await self._compute_blockers(db, cand)
                if blockers and all(b["task_id"] == task_id for b in blockers):
                    reblocked.append(cand)
            return reblocked

    async def list_tasks(
        self,
        agent: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        since: str | None = None,
        resolved_since: str | None = None,
        with_claims: bool = False,
        metadata_match: dict | None = None,
        task_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """List tasks with optional filters.

        Args:
            agent: Filter by created_by agent
            status: Filter by status (open/completed/cancelled), or None for all
            tags: Filter by tags (task must have all specified tags)
            metadata_match: Filter by metadata (AND across keys). For each
                ``key: q`` a task matches when its stored metadata value equals
                ``q`` or is a list containing ``q``. Pushed into SQL via
                ``json_extract``/``json_each`` (engine-evaluated, not a Python
                post-scan). Query values must be scalars.
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
            task_type: Filter by first-class task type (task/epic/gate).

        Returns:
            List of task dicts with id, title, description, status, task_type,
            created_by, created_at, resolved_at, tags, metadata, outcome, and
            (when with_claims) claims.
        """
        query = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []

        if agent:
            query += " AND created_by = ?"
            params.append(agent)

        if status:
            query += " AND status = ?"
            params.append(status)

        if task_type:
            query += " AND task_type = ?"
            params.append(task_type)

        if since:
            query += " AND created_at >= ?"
            params.append(since)

        if resolved_since:
            query += " AND resolved_at >= ?"
            params.append(resolved_since)

        md_clause, md_params = _metadata_match_clause(metadata_match)
        query += md_clause
        params.extend(md_params)

        query += " ORDER BY created_at DESC"

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()

            results: list[dict[str, Any]] = []
            for row in rows:
                task = _task_row_to_dict(row)
                # Filter by tags: task must contain all requested tags
                if tags and not all(t in task["tags"] for t in tags):
                    continue
                results.append(task)

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

        now = _format_datetime(datetime.now(UTC))
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
            expires_dt = _parse_datetime(row["expires_at"]) or datetime.now(UTC)
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

        now = _format_datetime(datetime.now(UTC))

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
                        claimed_at=_parse_datetime(row["claimed_at"]) or datetime.now(UTC),
                        expires_at=_parse_datetime(row["expires_at"]) or datetime.now(UTC),
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
                        task_type=task["task_type"] if "task_type" in task_row_keys else "task",
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

        now = datetime.now(UTC)
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

        now = datetime.now(UTC)
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
        now = _format_datetime(datetime.now(UTC))

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

    # ==================== Task Graph Operations ====================

    @traced("lithos.coordination.upsert_task_edge")
    async def upsert_task_edge(
        self,
        from_task_id: str,
        to_task_id: str,
        edge_type: str,
        agent: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Create or update a typed relation between two tasks.

        Args:
            from_task_id: Source task (e.g. the blocker / parent / source).
            to_task_id: Target task (e.g. the blocked / child / discovered task).
            edge_type: One of the edge types accepted this phase
                (:data:`ACCEPTED_EDGE_TYPES`).
            agent: Agent creating the edge.
            metadata: Optional edge metadata (merged-by-replace on conflict).

        Returns:
            True on success.

        Raises:
            CoordinationError: unaccepted edge type, self-edge, missing task, or a
                blocking edge that would close a dependency cycle.
        """
        import json

        if edge_type not in ACCEPTED_EDGE_TYPES:
            raise CoordinationError(
                "invalid_edge_type",
                f"edge type '{edge_type}' is not accepted in this phase "
                f"(accepted: {sorted(ACCEPTED_EDGE_TYPES)}).",
            )
        if from_task_id == to_task_id:
            raise CoordinationError("self_edge", "An edge cannot connect a task to itself.")

        lithos_metrics.coordination_ops.add(1, {"op": "upsert_task_edge"})
        await self.ensure_agent_known(agent)
        now = _format_datetime(datetime.now(UTC))
        metadata_json = json.dumps(metadata) if metadata is not None else None

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT id, task_type FROM tasks WHERE id IN (?, ?)",
                (from_task_id, to_task_id),
            )
            type_by_id = {row[0]: row[1] for row in await cursor.fetchall()}
            missing = [t for t in (from_task_id, to_task_id) if t not in type_by_id]
            if missing:
                raise CoordinationError(
                    "task_not_found", f"edge references nonexistent task(s): {missing}"
                )

            if edge_type in GATE_EDGE_TYPES and type_by_id.get(from_task_id) != "gate":
                # A waits_on_gate blocker must be a gate task — otherwise the
                # readiness predicate (which keys on gate metadata) cannot reason
                # about it and the waiter would never be released correctly.
                raise CoordinationError(
                    "not_a_gate",
                    f"a {edge_type} edge requires the from_task ({from_task_id}) to be a "
                    f"'gate' task, got task_type={type_by_id.get(from_task_id)!r}.",
                )

            if edge_type in DEPENDENCY_EDGE_TYPES:
                # Adding from->to means "to depends on from"; reject if from
                # already depends on to (reverse = up the depends-on chain).
                # Traverses the whole dependency graph (blocks + waits_on_gate) so
                # mixed cycles are caught.
                cycle = await self._find_edge_path(
                    db, from_task_id, to_task_id, DEPENDENCY_EDGE_TYPES, reverse=True
                )
                if cycle is not None:
                    raise CoordinationError(
                        "cycle",
                        f"{edge_type} edge "
                        f"{from_task_id} -> {to_task_id} would create a dependency cycle: "
                        f"{' -> '.join(cycle)} -> {from_task_id}",
                    )
            elif edge_type in HIERARCHY_EDGE_TYPES:
                # Forest invariant: at most one parent per task. Reject a second,
                # *different* parent; re-upserting the same parent->child edge is
                # fine (it just updates metadata via ON CONFLICT below).
                cursor = await db.execute(
                    "SELECT from_task_id FROM task_edges WHERE to_task_id = ? AND type = ?",
                    (to_task_id, edge_type),
                )
                other_parents = {row[0] for row in await cursor.fetchall()} - {from_task_id}
                if other_parents:
                    raise CoordinationError(
                        "parent_exists",
                        f"task {to_task_id} already has a parent ({next(iter(other_parents))}); a "
                        "task may have at most one parent. Remove the existing parent_child edge "
                        "before re-parenting.",
                    )
                # Adding parent(from)->child(to) is a cycle if the parent is
                # already a descendant of the child (reverse=False = down the
                # hierarchy from the child).
                cycle = await self._find_edge_path(
                    db, to_task_id, from_task_id, HIERARCHY_EDGE_TYPES, reverse=False
                )
                if cycle is not None:
                    raise CoordinationError(
                        "cycle",
                        f"parent_child edge {from_task_id} -> {to_task_id} would create a "
                        f"hierarchy cycle: {' -> '.join(cycle)} -> {to_task_id}",
                    )

            await db.execute(
                """
                INSERT INTO task_edges
                    (from_task_id, to_task_id, type, metadata, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(from_task_id, to_task_id, type)
                    DO UPDATE SET metadata = excluded.metadata
                """,
                (from_task_id, to_task_id, edge_type, metadata_json, agent, now),
            )
            await db.commit()

        logger.info(
            "Task edge upserted: from=%s to=%s type=%s agent=%s",
            from_task_id,
            to_task_id,
            edge_type,
            agent,
        )
        return True

    @traced("lithos.coordination.list_task_edges")
    async def list_task_edges(
        self,
        task_id: str,
        direction: Literal["incoming", "outgoing", "both"] = "both",
        types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """List edges touching ``task_id``.

        Each returned edge dict carries ``direction`` ("incoming" / "outgoing")
        relative to ``task_id``. Index-driven via idx_task_edges_from/to.
        """
        type_clause = ""
        type_params: list[Any] = []
        if types:
            placeholders = ",".join("?" for _ in types)
            type_clause = f" AND type IN ({placeholders})"
            type_params = list(types)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            edges: list[dict[str, Any]] = []
            if direction in ("outgoing", "both"):
                cursor = await db.execute(
                    f"SELECT * FROM task_edges WHERE from_task_id = ?{type_clause}",
                    (task_id, *type_params),
                )
                edges.extend(self._edge_row_to_dict(r, "outgoing") for r in await cursor.fetchall())
            if direction in ("incoming", "both"):
                cursor = await db.execute(
                    f"SELECT * FROM task_edges WHERE to_task_id = ?{type_clause}",
                    (task_id, *type_params),
                )
                edges.extend(self._edge_row_to_dict(r, "incoming") for r in await cursor.fetchall())
            return edges

    @staticmethod
    def _edge_row_to_dict(row: Any, direction: str) -> dict[str, Any]:
        """Build an edge payload dict from a ``task_edges`` row."""
        return {
            "from_task_id": row["from_task_id"],
            "to_task_id": row["to_task_id"],
            "type": row["type"],
            "direction": direction,
            "metadata": _decode_metadata(row["metadata"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    async def _find_edge_path(
        self,
        db: aiosqlite.Connection,
        start: str,
        target: str,
        edge_types: frozenset[str],
        reverse: bool = True,
    ) -> list[str] | None:
        """Return a path ``[start, ..., target]`` over ``edge_types`` or None.

        ``reverse=True`` follows edges *up* — the neighbours of ``X`` are the
        ``from_task_id`` of incoming edges (``to_task_id = X``), via
        idx_task_edges_to. This is the *depends-on* / *ancestor* direction used
        for blocking-cycle detection ("does ``start`` already depend on
        ``target``?") and cycle classification in :meth:`list_blocked`.

        ``reverse=False`` follows edges *down* — the neighbours of ``X`` are the
        ``to_task_id`` of outgoing edges (``from_task_id = X``), via
        idx_task_edges_from. This is the *descendant* direction used for
        parent_child cycle detection ("is ``target`` a descendant of ``start``?").

        Bounded by the reachable subgraph with a visited-set — never a
        full-table walk.
        """
        types = tuple(edge_types)
        placeholders = ",".join("?" for _ in types)
        if reverse:
            sql = f"SELECT from_task_id FROM task_edges WHERE to_task_id = ? AND type IN ({placeholders})"
        else:
            sql = f"SELECT to_task_id FROM task_edges WHERE from_task_id = ? AND type IN ({placeholders})"
        visited = {start}
        parent: dict[str, str] = {}
        stack = [start]
        while stack:
            node = stack.pop()
            cursor = await db.execute(sql, (node, *types))
            for (neighbour,) in await cursor.fetchall():
                if neighbour in visited:
                    continue
                parent[neighbour] = node
                if neighbour == target:
                    path = [target]
                    cur = node
                    while cur != start:
                        path.append(cur)
                        cur = parent[cur]
                    path.append(start)
                    path.reverse()
                    return path
                visited.add(neighbour)
                stack.append(neighbour)
        return None

    @staticmethod
    def _now_iso() -> str:
        """Canonical UTC second-precision ISO 'now' — matches the normalized
        ``ready_at`` stored on timer gates, so the comparison is exact."""
        return datetime.now(UTC).replace(microsecond=0).isoformat()

    @staticmethod
    def _unsatisfied_blocker_sql(now: str) -> tuple[str, list[Any]]:
        """SQL fragment + params for "incoming edge ``e`` (predecessor ``p``) is
        an unsatisfied blocker", the single source of truth for readiness.

        A ``blocks`` edge is unsatisfied while its predecessor is not ``completed``.
        A ``waits_on_gate`` edge is unsatisfied while the gate is not resolved —
        resolved means the gate is ``completed``, or it is an ``open`` ``timer``
        gate whose ``ready_at <= now``. A ``cancelled`` gate is therefore
        unsatisfied (cancelled wins over a timer), surfaced as ``blocker_unsatisfiable``.

        The timer auto-resolve is wrapped in ``COALESCE(..., 0)`` so that a
        predecessor with absent/invalid gate metadata (NULL ``json_extract``)
        defaults to **0 = not auto-resolved**, i.e. the dependent stays blocked
        rather than falling through as ready on SQLite NULL semantics. Combined
        with the write-time rule that a ``waits_on_gate`` blocker must be a gate,
        an unknown gate state never spuriously readies its waiter.
        """
        blocking = tuple(BLOCKING_EDGE_TYPES)
        gate = tuple(GATE_EDGE_TYPES)
        bph = ",".join("?" for _ in blocking)
        gph = ",".join("?" for _ in gate)
        fragment = (
            f"( (e.type IN ({bph}) AND p.status != 'completed')"
            f" OR (e.type IN ({gph}) AND p.status != 'completed'"
            "      AND NOT COALESCE((p.status = 'open'"
            "               AND json_extract(p.metadata, '$.gate_type') = 'timer'"
            "               AND json_extract(p.metadata, '$.ready_at') <= ?), 0)) )"
        )
        return fragment, [*blocking, *gate, now]

    async def _is_task_ready(self, db: aiosqlite.Connection, task_id: str) -> bool:
        """Whether a single task currently satisfies the readiness predicate."""
        non_workable = ",".join("?" for _ in NON_WORKABLE_TASK_TYPES)
        frag, fparams = self._unsatisfied_blocker_sql(self._now_iso())
        cursor = await db.execute(
            f"""
            SELECT 1 FROM tasks t
            WHERE t.id = ? AND t.status = 'open' AND t.task_type NOT IN ({non_workable})
              AND NOT EXISTS (
                SELECT 1 FROM task_edges e JOIN tasks p ON p.id = e.from_task_id
                WHERE e.to_task_id = t.id AND {frag})
            """,
            (task_id, *NON_WORKABLE_TASK_TYPES, *fparams),
        )
        return await cursor.fetchone() is not None

    @traced("lithos.coordination.list_ready")
    async def list_ready(
        self,
        project: str | None = None,
        tags: list[str] | None = None,
        metadata_match: dict | None = None,
        limit: int = 50,
        with_claims: bool = True,
    ) -> list[dict[str, Any]]:
        """Return open tasks whose blocking predecessors are all ``completed``.

        Restricts to the indexed ``status='open'`` frontier first, then applies an
        index-driven anti-join over blocking edges, so cost scales with the open
        frontier (not total task count). ``project`` is shorthand for
        ``metadata.project == project``. Claims are *attached* when ``with_claims``
        but never used to exclude a task — collision-correctness lives in the
        atomic claim, and claims are per-aspect.
        """
        if limit <= 0:
            return []
        rows = await self._frontier_rows(
            ready=True,
            project=project,
            metadata_match=metadata_match,
            sql_limit=None if tags else limit,
        )
        results = self._apply_tags_and_limit(rows, tags, limit)
        if with_claims and results:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                claims_by_task = await self._fetch_active_claims_for(db, [r["id"] for r in results])
            for task in results:
                task["claims"] = claims_by_task.get(task["id"], [])
        return results

    @traced("lithos.coordination.list_blocked")
    async def list_blocked(
        self,
        project: str | None = None,
        tags: list[str] | None = None,
        metadata_match: dict | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return open tasks that are not ready, each with structured blockers.

        Same filter surface as :meth:`list_ready`. Each task carries a
        ``blockers`` list whose entries have ``kind`` in
        ``task``/``blocker_unsatisfiable``/``cycle`` (gate kinds arrive in Phase 3).
        """
        if limit <= 0:
            return []
        rows = await self._frontier_rows(
            ready=False,
            project=project,
            metadata_match=metadata_match,
            sql_limit=None if tags else limit,
        )
        results = self._apply_tags_and_limit(rows, tags, limit)
        async with aiosqlite.connect(self.db_path) as db:
            for task in results:
                task["blockers"] = await self._compute_blockers(db, task["id"])
        return results

    async def _frontier_rows(
        self,
        ready: bool,
        project: str | None,
        metadata_match: dict | None,
        sql_limit: int | None = None,
    ) -> list[Any]:
        """Fetch open workable rows partitioned by the readiness anti-join.

        ``ready=True`` selects tasks with no unsatisfied blocking predecessor;
        ``ready=False`` selects those with at least one. Both ride on the indexed
        ``status='open'`` frontier and the task_edges indexes.

        ``sql_limit`` pushes ``LIMIT`` into SQL so the engine stops early. Callers
        pass it only when no Python-side ``tags`` post-scan follows (a tag filter
        would otherwise drop rows after the cap and under-fill the result).
        """
        effective_match = dict(metadata_match) if metadata_match else {}
        if project is not None:
            effective_match["project"] = project
        md_clause, md_params = _metadata_match_clause(effective_match or None, column="t.metadata")

        non_workable = ",".join("?" for _ in NON_WORKABLE_TASK_TYPES)
        frag, fparams = self._unsatisfied_blocker_sql(self._now_iso())
        exists_kw = "NOT EXISTS" if ready else "EXISTS"
        query = (
            "SELECT t.* FROM tasks t "
            f"WHERE t.status = 'open' AND t.task_type NOT IN ({non_workable}) "
            f"AND {exists_kw} ("
            "  SELECT 1 FROM task_edges e JOIN tasks p ON p.id = e.from_task_id"
            f"  WHERE e.to_task_id = t.id AND {frag})"
            f"{md_clause} ORDER BY t.created_at DESC"
        )
        params = [*NON_WORKABLE_TASK_TYPES, *fparams, *md_params]
        if sql_limit is not None:
            query += " LIMIT ?"
            params.append(sql_limit)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            return list(await cursor.fetchall())

    @staticmethod
    def _apply_tags_and_limit(
        rows: list[Any],
        tags: list[str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Tag-filter (post-scan over the open frontier) and cap to ``limit``.

        A non-positive ``limit`` yields no tasks (the append-then-check loop would
        otherwise return one). The MCP layer rejects ``limit < 1`` outright; this
        guard keeps direct service callers consistent — and consistent with the
        SQL ``LIMIT 0`` path — rather than off-by-one.
        """
        results: list[dict[str, Any]] = []
        if limit <= 0:
            return results
        for row in rows:
            task = _task_row_to_dict(row)
            if tags and not all(t in task["tags"] for t in tags):
                continue
            results.append(task)
            if len(results) >= limit:
                break
        return results

    async def _compute_blockers(
        self,
        db: aiosqlite.Connection,
        task_id: str,
    ) -> list[dict[str, Any]]:
        """Explain why ``task_id`` is blocked: one entry per unsatisfied predecessor.

        Uses the same ``_unsatisfied_blocker_sql`` predicate as readiness (one
        source of truth), so only genuinely-unsatisfied edges are returned and a
        timer-resolved gate never appears here. Each row is then classified.
        """
        frag, fparams = self._unsatisfied_blocker_sql(self._now_iso())
        cursor = await db.execute(
            f"""
            SELECT e.from_task_id, e.type, p.status, p.metadata
            FROM task_edges e JOIN tasks p ON p.id = e.from_task_id
            WHERE e.to_task_id = ? AND {frag}
            """,
            (task_id, *fparams),
        )
        blockers: list[dict[str, Any]] = []
        for pred_id, edge_type, pred_status, pred_metadata in await cursor.fetchall():
            is_gate = edge_type in GATE_EDGE_TYPES
            if pred_status == "cancelled":
                noun = "Gate" if is_gate else "Blocking predecessor"
                what = "gate" if is_gate else "predecessor"
                blockers.append(
                    {
                        "kind": "blocker_unsatisfiable",
                        "task_id": pred_id,
                        "type": edge_type,
                        "status": "cancelled",
                        "message": (
                            f"{noun} {pred_id} was cancelled; this task can never become ready "
                            f"without intervention (complete/re-open the {what}, re-route, or "
                            "cancel this subtree)."
                        ),
                    }
                )
                continue
            if is_gate:
                md = _decode_metadata(pred_metadata)
                gate_type = md.get("gate_type")
                detail = f" (ready_at={md.get('ready_at')})" if gate_type == "timer" else ""
                blockers.append(
                    {
                        "kind": "gate",
                        "task_id": pred_id,
                        "type": edge_type,
                        "status": pred_status,
                        "message": f"Waiting on {gate_type} gate {pred_id}{detail}.",
                    }
                )
                continue
            # blocks edge, predecessor open: distinguish a genuine wait from a cycle
            cycle = await self._find_edge_path(
                db, pred_id, task_id, BLOCKING_EDGE_TYPES, reverse=True
            )
            if cycle is not None:
                blockers.append(
                    {
                        "kind": "cycle",
                        "task_id": pred_id,
                        "type": edge_type,
                        "status": "open",
                        "message": "dependency cycle: " + " -> ".join(cycle) + f" -> {pred_id}",
                    }
                )
            else:
                blockers.append(
                    {
                        "kind": "task",
                        "task_id": pred_id,
                        "type": edge_type,
                        "status": "open",
                        "message": f"Waiting on predecessor {pred_id} to complete.",
                    }
                )
        return blockers

    @traced("lithos.coordination.newly_unblocked_by")
    async def newly_unblocked_by(self, task_id: str) -> list[str]:
        """Return ids of tasks that ``task_id`` blocked and that are now ready.

        Called by the MCP layer right after a successful completion. Each
        candidate is a ``blocks`` dependent or ``waits_on_gate`` waiter of
        ``task_id`` (so completing a gate surfaces its now-ready waiters); it is
        reported only if it now satisfies the readiness predicate.
        """
        dependency = tuple(DEPENDENCY_EDGE_TYPES)
        placeholders = ",".join("?" for _ in dependency)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                f"SELECT DISTINCT to_task_id FROM task_edges "
                f"WHERE from_task_id = ? AND type IN ({placeholders})",
                (task_id, *dependency),
            )
            candidates = [row[0] for row in await cursor.fetchall()]
            return [c for c in candidates if await self._is_task_ready(db, c)]

    @traced("lithos.coordination.list_children")
    async def list_children(
        self,
        task_id: str,
        recursive: bool = False,
        include_closed: bool = False,
    ) -> list[dict[str, Any]]:
        """Return child tasks of ``task_id`` via outgoing ``parent_child`` edges.

        ``recursive`` walks the full descendant subtree; a visited-set bounds the
        traversal (defence in depth — write-time cycle rejection already keeps the
        hierarchy acyclic). ``include_closed=False`` filters the *returned* rows to
        non-terminal (``open``) tasks while still traversing the whole subtree, so
        an open grandchild under a closed child is still surfaced. Order is by
        ``created_at`` within each parent's children.
        """
        hierarchy = tuple(HIERARCHY_EDGE_TYPES)
        placeholders = ",".join("?" for _ in hierarchy)
        results: list[dict[str, Any]] = []
        seen: set[str] = {task_id}
        frontier: deque[str] = deque([task_id])
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            while frontier:
                parent = frontier.popleft()
                cursor = await db.execute(
                    f"""
                    SELECT t.* FROM task_edges e JOIN tasks t ON t.id = e.to_task_id
                    WHERE e.from_task_id = ? AND e.type IN ({placeholders})
                    ORDER BY t.created_at ASC
                    """,
                    (parent, *hierarchy),
                )
                for row in await cursor.fetchall():
                    child = _task_row_to_dict(row)
                    if child["id"] in seen:
                        continue
                    seen.add(child["id"])
                    if include_closed or child["status"] == "open":
                        results.append(child)
                    if recursive:
                        frontier.append(child["id"])
        return results

    @traced("lithos.coordination.spawn_task")
    async def spawn_task(
        self,
        source_task_id: str,
        title: str,
        agent: str,
        description: str | None = None,
        relation_type: str = "discovered_from",
        inherit_project: bool = True,
        inherit_tags: bool = True,
        inherit_context: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Create a follow-on task linked to ``source_task_id``.

        Composes the existing validated primitives: resolve inherited fields from
        the source, ``create_task`` the follow-on (always ``task_type='task'``),
        then ``upsert_task_edge`` the relation ``source -> spawned``. ``blocks`` is
        cycle-safe because the spawned task is brand-new.

        Raises:
            CoordinationError: unknown source, invalid ``relation_type``, or a
                forbidden metadata key (surfaced by ``create_task``).
        """
        if relation_type not in ("discovered_from", "blocks"):
            raise CoordinationError(
                "invalid_relation_type",
                f"relation_type must be 'discovered_from' or 'blocks', got '{relation_type}'.",
            )
        _reject_scheduling_metadata(metadata)

        source = await self.get_task(source_task_id)
        if source is None:
            raise CoordinationError("task_not_found", f"source task '{source_task_id}' not found.")

        # Inherited fields first, explicit args override.
        inherited_meta: dict[str, Any] = {}
        if inherit_project and "project" in source.metadata:
            inherited_meta["project"] = source.metadata["project"]
        if inherit_context:
            for key in INHERITABLE_CONTEXT_KEYS:
                if key in source.metadata:
                    inherited_meta[key] = source.metadata[key]
        merged_meta = {**inherited_meta, **(metadata or {})}
        spawned_tags = list(source.tags) if inherit_tags else None

        new_id = await self.create_task(
            title=title,
            agent=agent,
            description=description,
            tags=spawned_tags,
            metadata=merged_meta or None,
            task_type="task",
        )
        await self.upsert_task_edge(source_task_id, new_id, relation_type, agent)
        logger.info(
            "Task spawned: task_id=%s source=%s relation=%s agent=%s",
            new_id,
            source_task_id,
            relation_type,
            agent,
        )
        return new_id

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
        now = _format_datetime(datetime.now(UTC))
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
        now = _format_datetime(datetime.now(UTC))
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
        now = _format_datetime(datetime.now(UTC))

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
