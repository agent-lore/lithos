"""Tests for US-001: LCMA frontmatter fields with lazy read-time defaults."""

from datetime import datetime, timezone
from pathlib import Path

import frontmatter as fm

from lithos.config import LithosConfig
from lithos.knowledge import (
    KnowledgeManager,
    KnowledgeMetadata,
    apply_lcma_defaults,
    derive_namespace,
)


class TestDeriveNamespace:
    """Tests for namespace derivation from relative path."""

    def test_file_at_root_returns_default(self):
        assert derive_namespace(Path("note.md")) == "default"

    def test_single_subdirectory(self):
        assert derive_namespace(Path("projects/note.md")) == "projects"

    def test_nested_subdirectory(self):
        assert derive_namespace(Path("projects/alpha/note.md")) == "projects/alpha"

    def test_deeply_nested(self):
        assert derive_namespace(Path("a/b/c/d/note.md")) == "a/b/c/d"


class TestApplyLcmaDefaults:
    """Tests for apply_lcma_defaults helper."""

    def _make_metadata(self, **overrides: object) -> KnowledgeMetadata:
        base = {
            "id": "test-id",
            "title": "Test",
            "author": "agent",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        base.update(overrides)
        return KnowledgeMetadata(**base)  # type: ignore[arg-type]

    def test_defaults_for_pre_lcma_note(self):
        """All LCMA fields get defaults when absent."""
        meta = self._make_metadata()
        apply_lcma_defaults(meta, Path("note.md"))

        assert meta.schema_version == 1
        assert meta.namespace == "default"
        assert meta.access_scope == "shared"
        assert meta.note_type == "observation"
        assert meta.status == "active"
        assert meta.summaries is None  # summaries left absent

    def test_schema_version_default(self):
        meta = self._make_metadata()
        apply_lcma_defaults(meta, Path("note.md"))
        assert meta.schema_version == 1

    def test_namespace_derived_from_path(self):
        meta = self._make_metadata()
        apply_lcma_defaults(meta, Path("agents/findings/note.md"))
        assert meta.namespace == "agents/findings"

    def test_access_scope_default(self):
        meta = self._make_metadata()
        apply_lcma_defaults(meta, Path("note.md"))
        assert meta.access_scope == "shared"

    def test_note_type_default(self):
        meta = self._make_metadata()
        apply_lcma_defaults(meta, Path("note.md"))
        assert meta.note_type == "observation"

    def test_status_default(self):
        meta = self._make_metadata()
        apply_lcma_defaults(meta, Path("note.md"))
        assert meta.status == "active"

    def test_explicit_namespace_overrides_derivation(self):
        """Explicit namespace in metadata is not overwritten."""
        meta = self._make_metadata(namespace="custom-ns")
        apply_lcma_defaults(meta, Path("other/dir/note.md"))
        assert meta.namespace == "custom-ns"

    def test_explicit_values_preserved(self):
        """All explicit LCMA values are preserved."""
        meta = self._make_metadata(
            schema_version=2,
            namespace="explicit",
            access_scope="task",
            note_type="summary",
            status="archived",
            summaries={"short": "s", "long": "l"},
        )
        apply_lcma_defaults(meta, Path("whatever/note.md"))
        assert meta.schema_version == 2
        assert meta.namespace == "explicit"
        assert meta.access_scope == "task"
        assert meta.note_type == "summary"
        assert meta.status == "archived"
        assert meta.summaries == {"short": "s", "long": "l"}


class TestFromDictLcmaFields:
    """Tests for KnowledgeMetadata.from_dict with LCMA fields."""

    def test_from_dict_without_lcma_fields(self):
        """from_dict without LCMA fields leaves them as None."""
        data = {"id": "abc", "title": "T", "author": "a"}
        meta = KnowledgeMetadata.from_dict(data)
        assert meta.schema_version is None
        assert meta.namespace is None
        assert meta.access_scope is None
        assert meta.note_type is None
        assert meta.status is None
        assert meta.summaries is None

    def test_from_dict_with_lcma_fields(self):
        """from_dict correctly unpacks LCMA fields."""
        data = {
            "id": "abc",
            "title": "T",
            "author": "a",
            "schema_version": 2,
            "namespace": "projects/alpha",
            "access_scope": "task",
            "note_type": "hypothesis",
            "status": "quarantined",
            "summaries": {"short": "brief", "long": "detailed"},
        }
        meta = KnowledgeMetadata.from_dict(data)
        assert meta.schema_version == 2
        assert meta.namespace == "projects/alpha"
        assert meta.access_scope == "task"
        assert meta.note_type == "hypothesis"
        assert meta.status == "quarantined"
        assert meta.summaries == {"short": "brief", "long": "detailed"}

    def test_from_dict_schema_version_non_numeric(self):
        """Non-numeric schema_version falls back to None."""
        data = {"id": "abc", "schema_version": "bad"}
        meta = KnowledgeMetadata.from_dict(data)
        assert meta.schema_version is None

    def test_from_dict_summaries_non_dict_ignored(self):
        """Non-dict summaries value is ignored."""
        data = {"id": "abc", "summaries": "not a dict"}
        meta = KnowledgeMetadata.from_dict(data)
        assert meta.summaries is None

    def test_from_dict_lcma_fields_not_in_extra(self):
        """LCMA field keys are not captured in extra."""
        data = {
            "id": "abc",
            "schema_version": 1,
            "namespace": "ns",
            "access_scope": "shared",
            "note_type": "observation",
            "status": "active",
            "summaries": {"short": "s"},
        }
        meta = KnowledgeMetadata.from_dict(data)
        for key in (
            "schema_version",
            "namespace",
            "access_scope",
            "note_type",
            "status",
            "summaries",
        ):
            assert key not in meta.extra


class TestToDictLcmaRoundTrip:
    """Tests for to_dict round-trip with LCMA fields."""

    def test_round_trip_with_lcma_fields(self):
        """to_dict -> from_dict preserves LCMA fields."""
        now = datetime.now(timezone.utc)
        meta = KnowledgeMetadata(
            id="rt-id",
            title="Round Trip",
            author="agent",
            created_at=now,
            updated_at=now,
            schema_version=2,
            namespace="projects/beta",
            access_scope="task",
            note_type="concept",
            status="archived",
            summaries={"short": "brief", "long": "detailed desc"},
        )
        d = meta.to_dict()
        restored = KnowledgeMetadata.from_dict(d)
        assert restored.schema_version == 2
        assert restored.namespace == "projects/beta"
        assert restored.access_scope == "task"
        assert restored.note_type == "concept"
        assert restored.status == "archived"
        assert restored.summaries == {"short": "brief", "long": "detailed desc"}

    def test_round_trip_without_lcma_fields(self):
        """to_dict -> from_dict for pre-LCMA note doesn't introduce LCMA keys."""
        now = datetime.now(timezone.utc)
        meta = KnowledgeMetadata(
            id="pre-lcma",
            title="Old Note",
            author="agent",
            created_at=now,
            updated_at=now,
        )
        d = meta.to_dict()
        # LCMA keys should not appear in serialized dict
        for key in (
            "schema_version",
            "namespace",
            "access_scope",
            "note_type",
            "status",
            "summaries",
        ):
            assert key not in d
        restored = KnowledgeMetadata.from_dict(d)
        assert restored.schema_version is None
        assert restored.namespace is None

    def test_to_dict_only_includes_set_lcma_fields(self):
        """to_dict omits LCMA fields that are None."""
        now = datetime.now(timezone.utc)
        meta = KnowledgeMetadata(
            id="partial",
            title="Partial",
            author="agent",
            created_at=now,
            updated_at=now,
            schema_version=1,
            namespace="ns",
            # access_scope, note_type, status, summaries left as None
        )
        d = meta.to_dict()
        assert d["schema_version"] == 1
        assert d["namespace"] == "ns"
        assert "access_scope" not in d
        assert "note_type" not in d
        assert "status" not in d
        assert "summaries" not in d


class TestLcmaReadTimeDefaults:
    """Tests for LCMA defaults applied at read time via KnowledgeManager."""

    async def test_read_pre_lcma_note_gets_defaults(self, knowledge_manager: KnowledgeManager):
        """Reading a pre-LCMA note materializes all LCMA defaults."""
        result = await knowledge_manager.create(
            title="Pre-LCMA Note",
            content="Old content",
            agent="test-agent",
        )
        doc, _ = await knowledge_manager.read(id=result.document.id)
        meta = doc.metadata

        assert meta.schema_version == 1
        assert meta.namespace == "default"  # file at root
        assert meta.access_scope == "shared"
        assert meta.note_type == "observation"
        assert meta.status == "active"
        assert meta.summaries is None

    async def test_read_note_in_subdirectory_namespace(self, test_config: LithosConfig):
        """Namespace derived from subdirectory path."""
        kp = test_config.storage.knowledge_path
        subdir = kp / "projects" / "alpha"
        subdir.mkdir(parents=True, exist_ok=True)

        # Write a pre-LCMA note manually
        note_path = subdir / "deep-note.md"
        post = fm.Post(
            "# Deep Note\n\nSome content",
            id="deep-id",
            title="Deep Note",
            author="agent",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            tags=[],
            aliases=[],
            confidence=1.0,
            contributors=[],
            source=None,
            supersedes=None,
            version=1,
        )
        note_path.write_text(fm.dumps(post))

        km = KnowledgeManager(test_config)
        doc, _ = await km.read(path="projects/alpha/deep-note.md")
        assert doc.metadata.namespace == "projects/alpha"

    async def test_explicit_namespace_in_frontmatter_preserved(self, test_config: LithosConfig):
        """Explicit namespace in frontmatter overrides path derivation."""
        kp = test_config.storage.knowledge_path
        note_path = kp / "some-note.md"
        post = fm.Post(
            "# Note\n\nContent",
            id="ns-override-id",
            title="Note",
            author="agent",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            tags=[],
            aliases=[],
            confidence=1.0,
            contributors=[],
            source=None,
            supersedes=None,
            version=1,
            namespace="custom-override",
        )
        note_path.write_text(fm.dumps(post))

        km = KnowledgeManager(test_config)
        doc, _ = await km.read(path="some-note.md")
        assert doc.metadata.namespace == "custom-override"

    async def test_sync_from_disk_applies_lcma_defaults(self, test_config: LithosConfig):
        """sync_from_disk also applies LCMA defaults."""
        kp = test_config.storage.knowledge_path
        note_path = kp / "sync-note.md"
        post = fm.Post(
            "# Sync Note\n\nContent",
            id="sync-id",
            title="Sync Note",
            author="agent",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            tags=[],
            aliases=[],
            confidence=1.0,
            contributors=[],
            source=None,
            supersedes=None,
            version=1,
        )
        note_path.write_text(fm.dumps(post))

        km = KnowledgeManager(test_config)
        doc = await km.sync_from_disk(Path("sync-note.md"))
        meta = doc.metadata

        assert meta.schema_version == 1
        assert meta.namespace == "default"
        assert meta.access_scope == "shared"
        assert meta.note_type == "observation"
        assert meta.status == "active"

    async def test_sync_from_disk_subdirectory_namespace(self, test_config: LithosConfig):
        """sync_from_disk derives namespace from subdirectory."""
        kp = test_config.storage.knowledge_path
        subdir = kp / "tasks"
        subdir.mkdir(parents=True, exist_ok=True)
        note_path = subdir / "task-note.md"
        post = fm.Post(
            "# Task Note\n\nContent",
            id="task-id",
            title="Task Note",
            author="agent",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            tags=[],
            aliases=[],
            confidence=1.0,
            contributors=[],
            source=None,
            supersedes=None,
            version=1,
        )
        note_path.write_text(fm.dumps(post))

        km = KnowledgeManager(test_config)
        doc = await km.sync_from_disk(Path("tasks/task-note.md"))
        assert doc.metadata.namespace == "tasks"
