"""AsyncSqliteStore — the shared persistent-connection lifecycle for Lithos's
async SQLite stores.

Extracted from ``StatsStore`` and ``EdgeStore``, which carried a near-verbatim
copy of this machinery (task 971f8892). Owns exactly one WAL-mode,
autocommit-per-statement ``aiosqlite`` connection reused across every operation
(#172), an op-lock that serialises access to that shared handle, corrupt-file
probe/quarantine on open, and self-healing reconnect when the handle goes dead
underneath a caller.

Subclasses supply the DDL (:attr:`SCHEMA`) and the file location
(:meth:`db_path`), and may override :meth:`_run_migrations` to run additive
column migrations after the schema is applied. Everything else — open/close,
``_session``, reconnect, probe/quarantine — lives here once. Log lines and the
corrupt-file backup name derive from ``type(self).__name__`` /
``self.db_path.name`` so a subclass needs no lifecycle code of its own.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import aiosqlite

from lithos.config import LithosConfig, get_config


class AsyncSqliteStore(abc.ABC):
    """Base class owning the shared async-SQLite connection lifecycle.

    One persistent connection per store instance, opened lazily and healed on
    failure. Subclasses provide :attr:`SCHEMA` and :meth:`db_path`.
    """

    #: DDL applied via ``executescript`` on :meth:`open`. Subclasses MUST set it.
    SCHEMA: ClassVar[str] = ""

    def __init__(self, config: LithosConfig | None = None) -> None:
        self._config = config
        self._opened = False
        # Persistent SQLite connection reused across ops (#172).
        self._db: aiosqlite.Connection | None = None
        # Serialises every operation on the shared connection.
        self._op_lock: asyncio.Lock | None = None

    @property
    def config(self) -> LithosConfig:
        return self._config or get_config()

    @property
    def _logger(self) -> logging.Logger:
        """Logger for lifecycle events, named for the *concrete* subclass's module.

        Keeps warnings under their original logger name (``lithos.edge_store`` /
        ``lithos.lcma.stats``) after the lifecycle moved to this base, so operator
        filters and dashboards keyed on those names still see them.
        """
        return logging.getLogger(type(self).__module__)

    @property
    @abc.abstractmethod
    def db_path(self) -> Path:
        """Filesystem location of this store's database (subclass-supplied)."""

    async def _run_migrations(self, db: aiosqlite.Connection) -> None:
        """Run additive schema migrations after ``SCHEMA`` is applied.

        Default no-op; overridden by stores that add columns to an existing
        table across versions.
        """
        return None

    async def open(self) -> None:
        """Ensure the database exists with the correct schema and a live connection.

        Idempotent — safe to call multiple times. If the file is corrupt it is
        quarantined and a fresh database is created. After this returns
        :attr:`_db` is a persistent ``aiosqlite.Connection`` in WAL mode that
        every method reuses (#172).
        """
        if self._opened:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        if self.db_path.exists():
            healthy = await self._probe(self.db_path)
            if not healthy:
                self._quarantine(self.db_path)

        # ``isolation_level=None`` puts the connection in autocommit-per-statement
        # mode. With one shared connection across many coroutines the default
        # implicit-transaction state would be shared too: one coroutine's DML
        # could join another's open tx, and either commit could flush the other's
        # pending writes. Autocommit makes single-statement writes self-contained;
        # multi-statement transactions are bracketed with explicit BEGIN/COMMIT
        # inside :meth:`_session`.
        db = await aiosqlite.connect(self.db_path, isolation_level=None)
        # Row access by name everywhere; positional indexing keeps working too.
        db.row_factory = aiosqlite.Row
        # WAL gives concurrent readers and bounded fsync cost; foreign-key
        # enforcement matches the constraints declared in SCHEMA.
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(self.SCHEMA)
        await self._run_migrations(db)
        self._db = db
        self._opened = True

    async def close(self) -> None:
        """Close the persistent connection. Idempotent."""
        if self._db is not None:
            await self._db.close()
            self._db = None
        self._opened = False

    async def _ensure_open(self) -> None:
        """Lazily (re-)open the database on first use, after close, or after a dead worker.

        Three recovery cases:

        1. Never opened — call :meth:`open`.
        2. :meth:`close` ran but the store was reused — re-open transparently.
        3. The aiosqlite worker thread is no longer running (the connection was
           closed externally, the loop was torn down underneath us, etc.) — drop
           the stale handle and re-open.

        A later operation may still find a "live" handle that has gone bad
        underneath us; those failures are healed in :meth:`_session` so the next
        call starts from a fresh connection automatically.
        """
        if self._opened and self._db is not None and getattr(self._db, "_running", True):
            return
        if self._db is not None and not getattr(self._db, "_running", True):
            self._logger.warning(
                "%s connection worker is no longer running; reopening %s",
                type(self).__name__,
                self.db_path,
            )
            # Drop the dead handle so open() reconstructs from scratch.
            self._db = None
        self._opened = False
        await self.open()

    async def reconnect(self) -> None:
        """Force a close + re-open of the underlying connection. Idempotent."""
        await self.close()
        await self.open()

    def _conn(self) -> aiosqlite.Connection:
        """Return the live connection. ``open()`` must have run first."""
        assert self._db is not None, f"{type(self).__name__}.open() must be called before use"
        return self._db

    def _operation_mutex(self) -> asyncio.Lock:
        """Return the lock that serialises store methods on the shared handle."""
        if self._op_lock is None:
            self._op_lock = asyncio.Lock()
        return self._op_lock

    @staticmethod
    def _is_recoverable_connection_error(exc: BaseException) -> bool:
        """Return True when *exc* indicates the persistent handle is no longer usable."""
        if not isinstance(exc, (ValueError, RuntimeError, aiosqlite.Error)):
            return False
        message = str(exc).lower()
        return any(
            fragment in message
            for fragment in (
                "closed database",
                "closed connection",
                "event loop is closed",
                "no active connection",
                "cannot operate on a closed database",
            )
        )

    async def _reconnect_after_error(self) -> None:
        """Drop the current handle and open a fresh one after a connection-liveness failure."""
        db = self._db
        self._db = None
        self._opened = False
        if db is not None:
            with contextlib.suppress(Exception):
                await db.close()
        await self.open()

    @contextlib.asynccontextmanager
    async def _session(self, *, transactional: bool = False) -> AsyncIterator[aiosqlite.Connection]:
        """Yield exclusive access to the shared connection, optionally in a transaction."""
        name = type(self).__name__
        async with self._operation_mutex():
            await self._ensure_open()
            db = self._conn()

            if not transactional:
                try:
                    yield db
                except Exception as exc:
                    if self._is_recoverable_connection_error(exc):
                        self._logger.warning(
                            "%s operation hit a dead connection; reopening %s",
                            name,
                            self.db_path,
                            exc_info=True,
                        )
                        await self._reconnect_after_error()
                    raise
                return

            try:
                await db.execute("BEGIN IMMEDIATE")
            except Exception as exc:
                if self._is_recoverable_connection_error(exc):
                    self._logger.warning(
                        "%s transaction could not begin; reopening %s",
                        name,
                        self.db_path,
                        exc_info=True,
                    )
                    await self._reconnect_after_error()
                raise

            try:
                yield db
            except Exception as exc:
                with contextlib.suppress(Exception):
                    await db.execute("ROLLBACK")
                if self._is_recoverable_connection_error(exc):
                    self._logger.warning(
                        "%s transaction lost its connection; reopening %s",
                        name,
                        self.db_path,
                        exc_info=True,
                    )
                    await self._reconnect_after_error()
                raise

            try:
                await db.execute("COMMIT")
            except Exception as exc:
                if self._is_recoverable_connection_error(exc):
                    self._logger.warning(
                        "%s transaction could not commit; reopening %s",
                        name,
                        self.db_path,
                        exc_info=True,
                    )
                    await self._reconnect_after_error()
                raise

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

    def _quarantine(self, path: Path) -> Path:
        """Rename a corrupt database file and return the backup path."""
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.corrupt-{timestamp}")
        suffix = 1
        while backup.exists():
            backup = path.with_name(f"{path.name}.corrupt-{timestamp}-{suffix}")
            suffix += 1
        path.rename(backup)
        self._logger.warning("Quarantined corrupt %s → %s", path.name, backup)
        return backup
