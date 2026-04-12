"""Schema migration registry and runner for Lithos note schemas.

Tracks which schema versions have been applied to notes and provides
an idempotent runner that can evolve note schemas over time.

Registry is stored as JSON at ``config.storage.lithos_store_path / 'migrations' / 'registry.json'``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lithos.knowledge import KnowledgeManager

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Migration registry
# ------------------------------------------------------------------


class MigrationRegistry:
    """Persistent registry tracking applied schema migrations.

    The registry file is a JSON document with the structure::

        {
            "current_version": 1,
            "applied": [
                {"from": 1, "to": 2, "name": "v1_to_v2", "applied_at": "..."}
            ]
        }
    """

    def __init__(self, registry_path: Path) -> None:
        self.registry_path = registry_path
        self._data: dict[str, object] = {}

    def _load(self) -> dict[str, object]:
        if self._data:
            return self._data
        if self.registry_path.exists():
            raw = json.loads(self.registry_path.read_text())
            assert isinstance(raw, dict)
            self._data = raw
        else:
            self._data = {"current_version": 1, "applied": []}
        return self._data

    def _save(self) -> None:
        data = self._load()
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text(json.dumps(data, indent=2) + "\n")

    @property
    def current_version(self) -> int:
        data = self._load()
        version = data["current_version"]
        assert isinstance(version, int)
        return version

    @property
    def applied(self) -> list[dict[str, object]]:
        data = self._load()
        entries = data["applied"]
        assert isinstance(entries, list)
        return entries

    def has_applied(self, name: str) -> bool:
        """Check whether a migration with *name* has already been applied."""
        return any(e.get("name") == name for e in self.applied)

    def record(self, *, from_version: int, to_version: int, name: str) -> None:
        """Record that a migration was applied and bump current_version."""
        data = self._load()
        applied = data["applied"]
        assert isinstance(applied, list)
        applied.append(
            {
                "from": from_version,
                "to": to_version,
                "name": name,
                "applied_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        data["current_version"] = to_version
        self._save()

    def initialize(self) -> None:
        """Create the registry file if it doesn't exist."""
        if not self.registry_path.exists():
            self._data = {"current_version": 1, "applied": []}
            self._save()
            logger.info("Initialized migration registry at %s", self.registry_path)


# ------------------------------------------------------------------
# Migration definitions
# ------------------------------------------------------------------

MigrationFn = Callable[["KnowledgeManager"], None]


def _v1_to_v2(_knowledge: KnowledgeManager) -> None:
    """No-op placeholder — v1-to-v2 handled by lazy defaults + write-back-on-touch."""


# Ordered list of migrations. Each entry: (from_version, to_version, name, function).
MIGRATIONS: list[tuple[int, int, str, MigrationFn]] = [
    (1, 2, "v1_to_v2", _v1_to_v2),
]


# ------------------------------------------------------------------
# Runner
# ------------------------------------------------------------------


def run_migrations(knowledge: KnowledgeManager, registry: MigrationRegistry) -> None:
    """Run all pending migrations in order.

    Idempotent — already-applied migrations are skipped.
    """
    for from_v, to_v, name, fn in MIGRATIONS:
        if registry.has_applied(name):
            logger.debug("Migration %s already applied, skipping", name)
            continue
        if registry.current_version != from_v:
            logger.debug(
                "Migration %s requires version %d but current is %d, skipping",
                name,
                from_v,
                registry.current_version,
            )
            continue
        logger.info("Applying migration %s (v%d → v%d)", name, from_v, to_v)
        fn(knowledge)
        registry.record(from_version=from_v, to_version=to_v, name=name)
        logger.info("Migration %s applied successfully", name)
