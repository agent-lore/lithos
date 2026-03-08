"""Tests for event emission from file watcher handle_file_change."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from lithos.events import NOTE_DELETED, NOTE_UPDATED
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
        server.search.index_document(doc)
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
        server.search.index_document(doc)
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
        server.search.index_document(doc)
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
        server.search.index_document(doc)
        server.graph.add_document(doc)

        file_path = server.config.storage.knowledge_path / doc.path
        file_path.unlink()

        server.event_bus.emit = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

        # Should not raise even though emit fails
        await server.handle_file_change(file_path, deleted=True)
