"""Tests for event emission from file watcher handle_file_change."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from lithos.events import NOTE_DELETED, NOTE_RENAMED, NOTE_UPDATED
from lithos.knowledge import KnowledgeManager
from lithos.server import LithosServer

pytestmark = pytest.mark.integration


class TestFileWatcherEventEmission:
    """Test that handle_file_change emits events for file operations."""

    async def test_file_modify_emits_note_updated(self, server: LithosServer) -> None:
        """A file create/modify triggers note.updated event."""
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
        await server.handle_file_change(file_path, deleted=False)

        event = queue.get_nowait()
        assert event.type == NOTE_UPDATED
        assert event.payload["path"] == str(doc.path)

    async def test_file_delete_emits_note_deleted(self, server: LithosServer) -> None:
        """A file deletion triggers note.deleted event."""
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

        await server.handle_file_change(file_path, deleted=True)

        event = queue.get_nowait()
        assert event.type == NOTE_DELETED
        assert event.payload["path"] == str(doc.path)

    async def test_non_markdown_file_emits_no_event(self, server: LithosServer) -> None:
        """Non-markdown files produce no event."""
        queue = server.event_bus.subscribe()

        await server.handle_file_change(
            server.config.storage.knowledge_path / "ignored.txt", deleted=False
        )

        assert queue.empty()

    async def test_outside_root_file_emits_no_event(self, server: LithosServer) -> None:
        """Files outside knowledge root produce no event."""
        queue = server.event_bus.subscribe()

        await server.handle_file_change(Path("/tmp/outside.md"), deleted=False)

        assert queue.empty()

    async def test_event_emission_failure_does_not_crash_watcher(
        self, server: LithosServer
    ) -> None:
        """If event emission raises, handle_file_change still succeeds."""
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
        await server.handle_file_change(file_path, deleted=False)

    async def test_delete_emission_failure_does_not_crash_watcher(
        self, server: LithosServer
    ) -> None:
        """If event emission raises on delete, handle_file_change still succeeds."""
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
        await server.handle_file_change(file_path, deleted=True)

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
        await server.handle_file_rename(src_path, dest_path)

        # Path mapping is now under the destination, doc id unchanged.
        assert server.knowledge.get_id_by_path(dest_rel) == original_id
        # The old path is no longer in the path → id mapping.
        assert server.knowledge.get_id_by_path(doc.path) is None
        # The renamed event fired with both paths.
        event = queue.get_nowait()
        assert event.type == NOTE_RENAMED
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

        await server.handle_file_rename(src_path, dest_path)

        # Old path lookup gone, new path lookup wired to the same node.
        assert server.graph._path_to_node.get(str(doc.path)) is None
        assert server.graph._path_to_node.get(str(dest_rel)) == doc.id

    async def test_file_change_update_rebuilds_graph_edges(self, server: LithosServer) -> None:
        """handle_file_change rebuilds graph edges when a file is modified."""
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
        await server.handle_file_change(file_path, deleted=False)

        assert not server.graph.has_edge(source.id, target_alpha.id)
        assert server.graph.has_edge(source.id, target_beta.id)
