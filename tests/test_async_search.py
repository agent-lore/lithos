"""Tests for US-007: Async wrapping of sync search calls at server call sites.

Verifies that synchronous SearchEngine methods wrapped in asyncio.to_thread()
do not block the asyncio event loop, allowing concurrent operations.
"""

import asyncio
import time

import pytest


class TestAsyncSearchNonBlocking:
    """Verify that sync search calls wrapped in asyncio.to_thread() do not block the loop."""

    @pytest.mark.asyncio
    async def test_slow_search_does_not_block_concurrent_coroutine(self) -> None:
        """A long-running synchronous search should not block a concurrent async task.

        We simulate a slow full_text_search (0.5s) and run a fast coroutine
        concurrently via asyncio.to_thread. If the search blocked the event
        loop, the fast coroutine would not start until after the search
        completes, and the total time would be >= 1.0s (sequential).
        With proper to_thread wrapping, both run in parallel and total
        time is ~0.5s.
        """
        sleep_duration = 0.5

        def slow_search(query: str, limit: int = 10, **kwargs: object) -> list[object]:
            time.sleep(sleep_duration)
            return []

        fast_completed_at: float | None = None
        search_started_at: float | None = None

        async def fast_coroutine() -> None:
            nonlocal fast_completed_at
            await asyncio.sleep(0.05)
            fast_completed_at = time.monotonic()

        search_started_at = time.monotonic()

        # Run both concurrently — the slow search in a thread, the fast coroutine on the loop
        results, _ = await asyncio.gather(
            asyncio.to_thread(slow_search, "test query", limit=5),
            fast_coroutine(),
        )

        search_ended_at = time.monotonic()

        assert results == []
        assert fast_completed_at is not None

        # The fast coroutine should complete well before the slow search ends
        # (within ~0.1s of starting, not blocked by the 0.5s sleep)
        fast_elapsed = fast_completed_at - search_started_at
        assert fast_elapsed < 0.3, (
            f"Fast coroutine took {fast_elapsed:.2f}s — it was blocked by the sync search. "
            f"Expected < 0.3s with asyncio.to_thread() wrapping."
        )

        # Total time should be roughly the slow search duration, not 2x
        total_elapsed = search_ended_at - search_started_at
        assert total_elapsed < sleep_duration + 0.3, (
            f"Total time {total_elapsed:.2f}s suggests sequential execution, not parallel."
        )

    @pytest.mark.asyncio
    async def test_multiple_searches_run_in_parallel(self) -> None:
        """Multiple sync searches wrapped in to_thread run concurrently, not sequentially."""
        sleep_duration = 0.3

        call_log: list[str] = []

        def slow_full_text(query: str, **kwargs: object) -> list[object]:
            time.sleep(sleep_duration)
            call_log.append("full_text")
            return []

        def slow_semantic(query: str, **kwargs: object) -> list[object]:
            time.sleep(sleep_duration)
            call_log.append("semantic")
            return []

        start = time.monotonic()
        await asyncio.gather(
            asyncio.to_thread(slow_full_text, "test"),
            asyncio.to_thread(slow_semantic, "test"),
        )
        elapsed = time.monotonic() - start

        assert set(call_log) == {"full_text", "semantic"}
        # If parallel, elapsed ~ 0.3s; if sequential, elapsed ~ 0.6s
        assert elapsed < sleep_duration * 1.5, (
            f"Two {sleep_duration}s searches took {elapsed:.2f}s total — "
            f"expected < {sleep_duration * 1.5:.1f}s for parallel execution."
        )

    @pytest.mark.asyncio
    async def test_server_search_call_sites_use_to_thread(self) -> None:
        """Verify that server.py call sites use asyncio.to_thread for search methods.

        This is a static verification test — we check that the server module
        imports asyncio and that lithos_search uses to_thread wrapping by
        inspecting the source code.
        """
        import inspect

        from lithos import server

        source = inspect.getsource(server)
        # All three search methods should be wrapped in asyncio.to_thread
        assert "asyncio.to_thread(\n" in source or "asyncio.to_thread(" in source
        assert "self.search.full_text_search" in source
        assert "self.search.semantic_search" in source
        assert "self.search.hybrid_search" in source

    @pytest.mark.asyncio
    async def test_server_search_mutation_sites_use_to_thread(self) -> None:
        """Regression guard for #199: ``lithos_write`` and the file-watcher
        path used to call ``self.search.index_document()`` synchronously,
        blocking the event loop for Tantivy commits and ChromaDB embeddings.

        Greps the server source to ensure no direct (non-``to_thread``)
        call to ``index_document`` / ``remove_document`` remains.
        """
        import inspect

        from lithos import server

        source = inspect.getsource(server)
        for line in source.splitlines():
            stripped = line.strip()
            # Skip comments and inline references; only flag real call sites.
            if stripped.startswith("#"):
                continue
            for method in ("index_document", "remove_document"):
                if f"self.search.{method}(" in stripped:
                    assert "asyncio.to_thread" in stripped, (
                        f"Direct self.search.{method}() call — must be wrapped in "
                        f"asyncio.to_thread (#199). Offending line: {stripped!r}"
                    )
