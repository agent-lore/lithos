"""Tests for event emission from the WatchIntake Module (ADR-0007)."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from lithos.events import NOTE_CREATED, NOTE_DELETED, NOTE_RENAMED, NOTE_UPDATED
from lithos.knowledge import KnowledgeManager
from lithos.server import LithosServer
from lithos.watch_intake import WATCHER_AGENT

pytestmark = pytest.mark.integration


class TestFileWatcherEventEmission:
    """Test that WatchIntake emits events for file operations with the
    ``agent="watcher"`` sentinel (ADR-0007)."""

    async def test_file_create_emits_note_created(self, server: LithosServer) -> None:
        """A brand-new .md file appearing on disk triggers note.created with agent=\"watcher\".

        Pins the create-side attribution: an externally-written file that
        is not yet in ``KnowledgeManager._id_to_path`` produces
        ``NOTE_CREATED`` (not ``NOTE_UPDATED``), and the event carries the
        watcher sentinel. The branch is otherwise only exercised by the
        telemetry counter test (``TestFileWatcherEventsCounter``), which
        does not subscribe to the event.
        """
        queue = server.event_bus.subscribe(event_types=[NOTE_CREATED])

        new_file = server.config.storage.knowledge_path / "brand-new-watcher.md"
        new_file.write_text(
            "---\ntitle: Brand New Watcher Doc\nagent: external\n---\nHello.\n"
        )

        await server.watch_intake.upsert_from_disk(new_file)

        event = queue.get_nowait()
        assert event.type == NOTE_CREATED
        assert event.agent == WATCHER_AGENT
        assert event.payload["path"] == "brand-new-watcher.md"

    async def test_file_modify_emits_note_updated(self, server: LithosServer) -> None:
        """A file create/modify triggers note.updated event with agent="watcher"."""
        doc = (
            await server.knowledge.create(
                title="Watcher Event Doc",
                content="Content for watcher event test.",
                agent="test-agent",
                path="watched",
            )
        ).document
        server.search.index(KnowledgeManager.to_indexable(doc))
        server.graph.add_document(doc)

        queue = server.event_bus.subscribe(event_types=[NOTE_UPDATED])

        file_path = server.config.storage.knowledge_path / doc.path
        await server.watch_intake.upsert_from_disk(file_path)

        event = queue.get_nowait()
        assert event.type == NOTE_UPDATED
        assert event.agent == WATCHER_AGENT
        assert event.payload["path"] == str(doc.path)

    async def test_file_delete_emits_note_deleted(self, server: LithosServer) -> None:
        """A file deletion triggers note.deleted event with agent="watcher"."""
        doc = (
            await server.knowledge.create(
                title="Watcher Delete Doc",
                content="Content to be deleted.",
                agent="test-agent",
                path="watched",
            )
        ).document
        server.search.index(KnowledgeManager.to_indexable(doc))
        server.graph.add_document(doc)

        queue = server.event_bus.subscribe(event_types=[NOTE_DELETED])

        file_path = server.config.storage.knowledge_path / doc.path
        file_path.unlink()

        await server.watch_intake.delete_from_disk(file_path)

        event = queue.get_nowait()
        assert event.type == NOTE_DELETED
        assert event.agent == WATCHER_AGENT
        assert event.payload["path"] == str(doc.path)

    async def test_delete_emits_after_knowledge_delete_completes(
        self, server: LithosServer
    ) -> None:
        """Verification test for ADR-0007: NOTE_DELETED fires AFTER
        KnowledgeManager.delete has cleared the indices.

        Pins the corrected ordering — capture-before-mutate (path→id
        resolved inside the lock), emit-after-mutate (event delivered to
        subscriber while the id is already absent from
        ``get_id_by_path`` and ``_meta_cache``). The previously-claimed
        emit-before-delete invariant is retracted.
        """
        doc = (
            await server.knowledge.create(
                title="Ordering Verification Doc",
                content="Pins ADR-0007 ordering.",
                agent="test-agent",
                path="watched",
            )
        ).document
        server.search.index(KnowledgeManager.to_indexable(doc))
        server.graph.add_document(doc)

        queue = server.event_bus.subscribe(event_types=[NOTE_DELETED])

        file_path = server.config.storage.knowledge_path / doc.path
        file_path.unlink()

        await server.watch_intake.delete_from_disk(file_path)

        # Subscriber consumes the event with a valid payload["id"]…
        event = queue.get_nowait()
        assert event.type == NOTE_DELETED
        assert event.agent == WATCHER_AGENT
        assert event.payload["id"] == doc.id

        # …while KnowledgeManager already reports the id as removed.
        assert server.knowledge.get_id_by_path(doc.path) is None
        assert doc.id not in server.knowledge._meta_cache

    async def test_non_markdown_file_emits_no_event(self, server: LithosServer) -> None:
        """Non-markdown files produce no event."""
        queue = server.event_bus.subscribe()

        await server.watch_intake.upsert_from_disk(
            server.config.storage.knowledge_path / "ignored.txt"
        )

        assert queue.empty()

    async def test_outside_root_file_emits_no_event(self, server: LithosServer) -> None:
        """Files outside knowledge root produce no event."""
        queue = server.event_bus.subscribe()

        await server.watch_intake.upsert_from_disk(Path("/tmp/outside.md"))

        assert queue.empty()

    async def test_event_emission_failure_does_not_crash_watcher(
        self, server: LithosServer
    ) -> None:
        """If event emission raises, upsert_from_disk still succeeds."""
        doc = (
            await server.knowledge.create(
                title="Watcher Resilience Doc",
                content="Content for resilience test.",
                agent="test-agent",
                path="watched",
            )
        ).document
        server.search.index(KnowledgeManager.to_indexable(doc))
        server.graph.add_document(doc)

        # Replace event_bus.emit with a mock that raises
        server.event_bus.emit = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

        file_path = server.config.storage.knowledge_path / doc.path
        # Should not raise even though emit fails
        await server.watch_intake.upsert_from_disk(file_path)

    async def test_delete_emission_failure_does_not_crash_watcher(
        self, server: LithosServer
    ) -> None:
        """If event emission raises on delete, delete_from_disk still succeeds."""
        doc = (
            await server.knowledge.create(
                title="Watcher Delete Resilience",
                content="Content for delete resilience test.",
                agent="test-agent",
                path="watched",
            )
        ).document
        server.search.index(KnowledgeManager.to_indexable(doc))
        server.graph.add_document(doc)

        file_path = server.config.storage.knowledge_path / doc.path
        file_path.unlink()

        server.event_bus.emit = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

        # Should not raise even though emit fails
        await server.watch_intake.delete_from_disk(file_path)

    async def test_file_rename_preserves_doc_id_and_emits_renamed(
        self, server: LithosServer
    ) -> None:
        """An external rename keeps the doc id and emits ``note.renamed`` (#202)."""
        doc = (
            await server.knowledge.create(
                title="Renamable Doc",
                content="Body that survives a rename.",
                agent="test-agent",
                path="watched",
            )
        ).document
        server.search.index(KnowledgeManager.to_indexable(doc))
        server.graph.add_document(doc)

        original_id = doc.id
        knowledge_path = server.config.storage.knowledge_path
        src_path = knowledge_path / doc.path
        dest_rel = doc.path.with_name("renamed-doc.md")
        dest_path = knowledge_path / dest_rel
        src_path.rename(dest_path)

        queue = server.event_bus.subscribe(event_types=[NOTE_RENAMED])
        await server.watch_intake.rename_on_disk(src_path, dest_path)

        # Path mapping is now under the destination, doc id unchanged.
        assert server.knowledge.get_id_by_path(dest_rel) == original_id
        # The old path is no longer in the path → id mapping.
        assert server.knowledge.get_id_by_path(doc.path) is None
        # The renamed event fired with both paths.
        event = queue.get_nowait()
        assert event.type == NOTE_RENAMED
        assert event.agent == WATCHER_AGENT
        assert event.payload["id"] == original_id
        assert event.payload["src_path"] == str(doc.path)
        assert event.payload["dest_path"] == str(dest_rel)

    async def test_file_rename_updates_graph_path(self, server: LithosServer) -> None:
        """Renamed files end up in the graph under the new path lookup (#202)."""
        doc = (
            await server.knowledge.create(
                title="Graph Rename Doc",
                content="Linked from elsewhere via [[graph-rename-doc]] won't matter for path lookup.",
                agent="test-agent",
                path="watched",
            )
        ).document
        server.search.index(KnowledgeManager.to_indexable(doc))
        server.graph.add_document(doc)

        knowledge_path = server.config.storage.knowledge_path
        src_path = knowledge_path / doc.path
        dest_rel = doc.path.with_name("graph-renamed.md")
        dest_path = knowledge_path / dest_rel
        src_path.rename(dest_path)

        await server.watch_intake.rename_on_disk(src_path, dest_path)

        # Old path lookup gone, new path lookup wired to the same node.
        assert server.graph._path_to_node.get(str(doc.path)) is None
        assert server.graph._path_to_node.get(str(dest_rel)) == doc.id

    async def test_file_change_update_rebuilds_graph_edges(self, server: LithosServer) -> None:
        """upsert_from_disk rebuilds graph edges when a file is modified."""
        target_alpha = (
            await server.knowledge.create(
                title="Target Alpha",
                content="Alpha target document.",
                agent="test-agent",
                path="watched",
            )
        ).document
        target_beta = (
            await server.knowledge.create(
                title="Target Beta",
                content="Beta target document.",
                agent="test-agent",
                path="watched",
            )
        ).document
        server.graph.add_document(target_alpha)
        server.graph.add_document(target_beta)

        source = (
            await server.knowledge.create(
                title="Source Doc",
                content="Links to [[target-alpha]].",
                agent="test-agent",
                path="watched",
            )
        ).document
        server.graph.add_document(source)

        assert server.graph.has_edge(source.id, target_alpha.id)

        # Update the file on disk to link to target-beta instead, but skip graph.add_document
        # to simulate a file-watcher-only update path
        await server.knowledge.update(
            id=source.id,
            agent="test-agent",
            content="Now links to [[target-beta]].",
        )

        file_path = server.config.storage.knowledge_path / source.path
        await server.watch_intake.upsert_from_disk(file_path)

        assert not server.graph.has_edge(source.id, target_alpha.id)
        assert server.graph.has_edge(source.id, target_beta.id)
