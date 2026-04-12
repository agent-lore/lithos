"""Tests for LCMA schema migration registry and runner."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lithos.lcma.migrations import MigrationRegistry, run_migrations


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    """Return a path for a fresh registry file."""
    return tmp_path / "migrations" / "registry.json"


@pytest.fixture
def registry(registry_path: Path) -> MigrationRegistry:
    """Create a fresh MigrationRegistry."""
    return MigrationRegistry(registry_path)


class TestMigrationRegistryCreation:
    """Fresh registry creation and initialization."""

    def test_initialize_creates_file(self, registry: MigrationRegistry) -> None:
        assert not registry.registry_path.exists()
        registry.initialize()
        assert registry.registry_path.exists()

    def test_initialize_sets_version_1(self, registry: MigrationRegistry) -> None:
        registry.initialize()
        data = json.loads(registry.registry_path.read_text())
        assert data["current_version"] == 1
        assert data["applied"] == []

    def test_initialize_is_idempotent(self, registry: MigrationRegistry) -> None:
        registry.initialize()
        # Record a migration
        registry.record(from_version=1, to_version=2, name="v1_to_v2")
        # Initialize again — should not overwrite
        registry.initialize()
        assert registry.current_version == 2
        assert len(registry.applied) == 1

    def test_current_version_defaults_to_1(self, registry: MigrationRegistry) -> None:
        assert registry.current_version == 1

    def test_applied_defaults_to_empty(self, registry: MigrationRegistry) -> None:
        assert registry.applied == []


class TestMigrationRegistryRecord:
    """Recording applied migrations."""

    def test_record_bumps_version(self, registry: MigrationRegistry) -> None:
        registry.initialize()
        registry.record(from_version=1, to_version=2, name="v1_to_v2")
        assert registry.current_version == 2

    def test_record_appends_to_applied(self, registry: MigrationRegistry) -> None:
        registry.initialize()
        registry.record(from_version=1, to_version=2, name="v1_to_v2")
        assert len(registry.applied) == 1
        entry = registry.applied[0]
        assert entry["from"] == 1
        assert entry["to"] == 2
        assert entry["name"] == "v1_to_v2"
        assert "applied_at" in entry

    def test_record_persists_to_disk(self, registry: MigrationRegistry) -> None:
        registry.initialize()
        registry.record(from_version=1, to_version=2, name="v1_to_v2")
        # Re-read from disk
        registry2 = MigrationRegistry(registry.registry_path)
        assert registry2.current_version == 2
        assert len(registry2.applied) == 1

    def test_has_applied(self, registry: MigrationRegistry) -> None:
        registry.initialize()
        assert not registry.has_applied("v1_to_v2")
        registry.record(from_version=1, to_version=2, name="v1_to_v2")
        assert registry.has_applied("v1_to_v2")


class TestRunMigrations:
    """run_migrations applies pending migrations idempotently."""

    def test_applies_pending_migration(self, registry: MigrationRegistry) -> None:
        registry.initialize()
        knowledge = MagicMock()
        run_migrations(knowledge, registry)
        assert registry.current_version == 2
        assert registry.has_applied("v1_to_v2")

    def test_idempotent_rerun(self, registry: MigrationRegistry) -> None:
        registry.initialize()
        knowledge = MagicMock()
        run_migrations(knowledge, registry)
        # Run again — should be a no-op
        run_migrations(knowledge, registry)
        assert registry.current_version == 2
        assert len(registry.applied) == 1

    def test_skips_already_applied(self, registry: MigrationRegistry) -> None:
        registry.initialize()
        registry.record(from_version=1, to_version=2, name="v1_to_v2")
        knowledge = MagicMock()
        run_migrations(knowledge, registry)
        # Should still be version 2 with only one applied entry
        assert registry.current_version == 2
        assert len(registry.applied) == 1


class TestRegistryLocation:
    """Registry respects configured storage path."""

    def test_registry_path_is_configurable(self, tmp_path: Path) -> None:
        custom_path = tmp_path / "custom" / "location" / "registry.json"
        registry = MigrationRegistry(custom_path)
        registry.initialize()
        assert custom_path.exists()

    def test_registry_structure(self, registry: MigrationRegistry) -> None:
        registry.initialize()
        registry.record(from_version=1, to_version=2, name="v1_to_v2")
        data = json.loads(registry.registry_path.read_text())
        assert "current_version" in data
        assert "applied" in data
        assert isinstance(data["applied"], list)
        assert data["applied"][0]["from"] == 1
        assert data["applied"][0]["to"] == 2
        assert data["applied"][0]["name"] == "v1_to_v2"
        assert "applied_at" in data["applied"][0]
