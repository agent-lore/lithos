"""Watch intake — filesystem-driven Corpus mutations.

``WatchIntake`` is the peer of ``CorpusIntake``: the agent-driven seam handles
mutations originating in MCP tool calls; the filesystem-driven seam handles
mutations originating in external file events (Obsidian, ``mv``, ``git checkout``,
bulk import scripts). See ADR-0007 for the design rationale and rejected
alternatives.

The Module owns three filesystem operations and the watchdog ``Observer``
lifecycle:

    * ``upsert_from_disk``   — file appeared or changed on disk;
    * ``delete_from_disk``   — file disappeared from disk;
    * ``rename_on_disk``     — file moved (in-corpus, into corpus, or out of
                                 corpus; pure-outside moves are no-ops).

All three serialise the path→id capture step (and the in-corpus rename
sequence) on a private ``_update_lock``. The lock is local to the Module —
``KnowledgeManager`` has its own ``_write_lock`` for atomicity of the
mutation itself; ``_update_lock`` exists to serialise the watcher path
because watchdog can deliver duplicate events from the OS, not because the
underlying mutation needs an outer lock.

Watcher-emitted events carry ``agent="watcher"`` (system-reserved sentinel).
Today's empty-string ``agent`` was a negative distinguisher; the sentinel is
the affirmative form. No subscriber filters on ``event.agent`` today, so the
change is backward-compatible at the consumer surface.

The ``delete_from_disk`` emit order matches ``CorpusIntake.delete``:
``KnowledgeManager.delete`` → ``search.remove`` → ``graph.remove_document``
→ emit. The previously-claimed "emit-before-delete is load-bearing" framing
is retracted in ADR-0007: ``EventBus.emit`` is queue-based, no subscriber
observes pre-delete cache state regardless of emit order. The real invariant
is capture-before-mutate — ``get_id_by_path(relative_path)`` must run before
``KnowledgeManager.delete(id)`` because ``delete`` clears ``_id_to_path``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from lithos.events import (
    NOTE_CREATED,
    NOTE_DELETED,
    NOTE_RENAMED,
    NOTE_UPDATED,
    EventBus,
    LithosEvent,
)
from lithos.graph import KnowledgeGraph
from lithos.knowledge import KnowledgeManager
from lithos.search import SearchEngine
from lithos.telemetry import get_tracer, lithos_metrics

logger = logging.getLogger(__name__)

# Agent attribution sentinel for events originating in the file watcher
# (ADR-0007). Use the affirmative sentinel over today's empty-string
# distinguisher so consumers can match on it without relying on truthiness.
WATCHER_AGENT = "watcher"


class WatchIntake:
    """Filesystem-driven Corpus mutations. Peer of :class:`CorpusIntake`.

    Constructor takes the four view-layer collaborators and the watch path.
    No ``coordination`` dependency — watcher-driven mutations do not register
    agents because there is no agent. The Module holds a private
    ``_update_lock`` that wraps the path→id capture on ``delete_from_disk``
    and serialises the in-corpus rename sequence on ``rename_on_disk``; it
    also owns the watchdog ``Observer`` and the private
    ``_FileChangeHandler`` adapter.
    """

    def __init__(
        self,
        knowledge: KnowledgeManager,
        search: SearchEngine,
        graph: KnowledgeGraph,
        event_bus: EventBus,
        watch_path: Path,
    ) -> None:
        self._knowledge = knowledge
        self._search = search
        self._graph = graph
        self._event_bus = event_bus
        self._watch_path = watch_path
        self._update_lock = asyncio.Lock()
        self._observer: Observer | None = None  # type: ignore[reportInvalidTypeForm]
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the watchdog ``Observer`` watching ``watch_path`` recursively.

        Idempotent: a second call while the observer is running is a no-op.
        The loop is captured so the private ``_FileChangeHandler`` can
        marshal watchdog-thread events back onto the asyncio loop via
        :func:`asyncio.run_coroutine_threadsafe`.
        """
        if self._observer is not None:
            return
        self._loop = loop
        handler = WatchIntake._FileChangeHandler(self, loop)
        observer = Observer()
        observer.schedule(handler, str(self._watch_path), recursive=True)
        observer.start()
        self._observer = observer

    async def stop(self) -> None:
        """Stop the watchdog ``Observer`` and join its thread.

        Idempotent: a second call after stop is a no-op. The graph-cache
        flush that lived in the old ``stop_file_watcher`` is now a
        graph-cache concern owned by :meth:`LithosServer.shutdown` (ADR-0007).
        """
        if self._observer is None:
            return
        self._observer.stop()
        self._observer.join()
        self._observer = None
        self._loop = None

    async def upsert_from_disk(self, path: Path) -> None:
        """Apply a filesystem create-or-modify to the Corpus and derived views.

        Non-markdown files and paths outside ``watch_path`` are ignored.
        On success the document is re-read from disk via
        ``KnowledgeManager.sync_from_disk``, re-indexed in Search (awaited via
        ``asyncio.to_thread``), and re-bound in the link graph (sync;
        debounces own flush). The ``NOTE_CREATED`` / ``NOTE_UPDATED`` event
        fires after both view syncs have been kicked off, matching
        ``CorpusIntake.write``'s order. All emits carry ``agent="watcher"``.
        """
        if path.suffix != ".md":
            return

        tracer = get_tracer()
        with tracer.start_as_current_span("lithos.watch_intake.upsert") as span:
            span.set_attribute("lithos.deleted", False)
            async with self._update_lock:
                try:
                    try:
                        relative_path = path.relative_to(self._watch_path)
                    except ValueError:
                        return

                    is_new = not self._knowledge.get_id_by_path(relative_path)
                    doc = await self._knowledge.sync_from_disk(relative_path)
                    indexable = KnowledgeManager.to_indexable(doc)
                    await asyncio.to_thread(self._search.index, indexable)
                    # graph.add_document() debounces its own flush (#203)
                    self._graph.add_document(doc)

                    event_type = "created" if is_new else "updated"
                    lithos_metrics.file_watcher_events.add(1, {"event_type": event_type})
                    await self._emit(
                        LithosEvent(
                            type=NOTE_CREATED if is_new else NOTE_UPDATED,
                            agent=WATCHER_AGENT,
                            payload={"path": str(relative_path)},
                        )
                    )
                except Exception as e:
                    logger.error("Error handling file change %s: %s", path, e)

    async def delete_from_disk(self, path: Path) -> None:
        """Apply a filesystem deletion to the Corpus and derived views.

        Non-markdown files and paths outside ``watch_path`` are ignored.
        Capture-before-mutate is enforced: ``get_id_by_path`` runs inside
        ``_update_lock`` before ``KnowledgeManager.delete``, because ``delete``
        clears ``_id_to_path`` (ADR-0007). On success the document is
        removed from KnowledgeManager, Search, and the link graph, and
        ``NOTE_DELETED`` (carrying ``agent="watcher"``) fires after both
        view syncs have been kicked off — matching ``CorpusIntake.delete``'s
        emit-after-mutate order. The previously-claimed emit-before-delete
        invariant is retracted (ADR-0007).
        """
        if path.suffix != ".md":
            return

        tracer = get_tracer()
        with tracer.start_as_current_span("lithos.watch_intake.delete") as span:
            span.set_attribute("lithos.deleted", True)
            async with self._update_lock:
                try:
                    try:
                        relative_path = path.relative_to(self._watch_path)
                    except ValueError:
                        return

                    # Capture-before-mutate: KnowledgeManager.delete clears
                    # _id_to_path, so the path→id resolution must precede it.
                    doc_id = self._knowledge.get_id_by_path(relative_path)
                    if not doc_id:
                        return

                    await self._knowledge.delete(doc_id)
                    await asyncio.to_thread(self._search.remove, doc_id)
                    # graph.remove_document() debounces its own flush (#203)
                    self._graph.remove_document(doc_id)

                    lithos_metrics.file_watcher_events.add(1, {"event_type": "deleted"})
                    await self._emit(
                        LithosEvent(
                            type=NOTE_DELETED,
                            agent=WATCHER_AGENT,
                            payload={"id": doc_id, "path": str(relative_path)},
                        )
                    )
                except Exception as e:
                    logger.error("Error handling file delete %s: %s", path, e)

    async def rename_on_disk(self, src: Path, dest: Path) -> None:
        """Apply an external rename (#202) to the Corpus and derived views.

        Behaviour by source/destination location relative to ``watch_path``:

        * both inside  → rename in place, emit ``NOTE_RENAMED``;
        * source inside, destination outside → degrade to
          ``delete_from_disk(src)``;
        * source outside, destination inside → degrade to
          ``upsert_from_disk(dest)``;
        * both outside → no-op.

        In-place renames re-bind the existing doc id under the new path via
        ``sync_from_disk`` (the graph and Search backends overwrite by id),
        preserving wiki-link targets that previously went stale on
        delete+create. The ``_update_lock`` serialises the in-corpus rename;
        the degradation branches acquire the lock through the inner method.
        """

        def _relative_or_none(p: Path) -> Path | None:
            try:
                return p.relative_to(self._watch_path)
            except ValueError:
                return None

        src_rel = _relative_or_none(src) if src.suffix == ".md" else None
        dest_rel = _relative_or_none(dest) if dest.suffix == ".md" else None

        if src_rel is None and dest_rel is None:
            return
        if src_rel is None and dest_rel is not None:
            await self.upsert_from_disk(dest)
            return
        if src_rel is not None and dest_rel is None:
            await self.delete_from_disk(src)
            return

        assert src_rel is not None
        assert dest_rel is not None

        tracer = get_tracer()
        with tracer.start_as_current_span("lithos.watch_intake.rename") as span:
            span.set_attribute("lithos.src_path", str(src_rel))
            span.set_attribute("lithos.dest_path", str(dest_rel))
            # FileNotFoundError degrades to delete_from_disk, which acquires
            # _update_lock itself — defer that recursion until after we've
            # released the lock here (asyncio.Lock is not reentrant).
            degrade_to_delete = False
            async with self._update_lock:
                try:
                    doc_id = self._knowledge.get_id_by_path(src_rel)
                    # sync_from_disk re-reads frontmatter and rebinds the
                    # path mapping under the existing doc id. The graph
                    # and search backends overwrite by id, so re-indexing
                    # closes the loop without losing wiki-link targets.
                    doc = await self._knowledge.sync_from_disk(dest_rel)
                    indexable = KnowledgeManager.to_indexable(doc)
                    await asyncio.to_thread(self._search.index, indexable)
                    self._graph.add_document(doc)

                    lithos_metrics.file_watcher_events.add(1, {"event_type": "renamed"})
                    await self._emit(
                        LithosEvent(
                            type=NOTE_RENAMED,
                            agent=WATCHER_AGENT,
                            payload={
                                "id": doc_id or doc.id,
                                "src_path": str(src_rel),
                                "dest_path": str(dest_rel),
                            },
                        )
                    )
                except FileNotFoundError:
                    # Destination disappeared between the watchdog event
                    # and our processing — treat as a deletion of the
                    # source.
                    degrade_to_delete = True
                except Exception as exc:
                    logger.error("Error handling file rename %s -> %s: %s", src, dest, exc)

            if degrade_to_delete:
                await self.delete_from_disk(src)

    async def _emit(self, event: LithosEvent) -> None:
        """Emit an event, logging any failure without propagating.

        Mirrors ``CorpusIntake._emit``: a failed event delivery never undoes
        a successful Corpus mutation.
        """
        try:
            await self._event_bus.emit(event)
        except Exception:
            logger.exception("Failed to emit %s event", event.type)

    class _FileChangeHandler(FileSystemEventHandler):
        """Handle watchdog file system events for index updates.

        Lives in the watchdog observer thread; marshals each event back to
        the asyncio loop captured at :meth:`WatchIntake.start` via
        :func:`asyncio.run_coroutine_threadsafe`. Schedule errors are
        swallowed to keep the watcher running; coroutine-level errors are
        logged via :meth:`_log_future_exception`.
        """

        def __init__(self, intake: WatchIntake, loop: asyncio.AbstractEventLoop) -> None:
            self.intake = intake
            self._loop = loop

        def on_created(self, event: FileSystemEvent) -> None:
            if not event.is_directory:
                self._schedule_update(Path(str(event.src_path)))

        def on_modified(self, event: FileSystemEvent) -> None:
            if not event.is_directory:
                self._schedule_update(Path(str(event.src_path)))

        def on_deleted(self, event: FileSystemEvent) -> None:
            if not event.is_directory:
                self._schedule_update(Path(str(event.src_path)), deleted=True)

        def on_moved(self, event: FileSystemEvent) -> None:
            """Handle external file renames (#202).

            Watchdog emits this on most platforms (POSIX inotify, macOS
            FSEvents, Windows ReadDirectoryChangesW). Some network
            filesystems do not — in that case watchdog falls back to a
            delete+create pair which still works through the existing
            handlers, just less precisely.
            """
            if event.is_directory:
                return
            dest_path = getattr(event, "dest_path", None)
            if dest_path is None:
                return
            self._schedule_rename(Path(str(event.src_path)), Path(str(dest_path)))

        def _schedule_update(self, path: Path, deleted: bool = False) -> None:
            try:
                coro = (
                    self.intake.delete_from_disk(path)
                    if deleted
                    else self.intake.upsert_from_disk(path)
                )
                future = asyncio.run_coroutine_threadsafe(coro, self._loop)
                future.add_done_callback(self._log_future_exception)
            except Exception:
                pass

        def _schedule_rename(self, src_path: Path, dest_path: Path) -> None:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self.intake.rename_on_disk(src_path, dest_path),
                    self._loop,
                )
                future.add_done_callback(self._log_future_exception)
            except Exception:
                pass

        @staticmethod
        def _log_future_exception(future: concurrent.futures.Future[None]) -> None:
            try:
                exception = future.exception()
                if exception:
                    logger.error("Error processing file update: %s", exception)
            except Exception:
                pass
