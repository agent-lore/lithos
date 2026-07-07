"""Tests for US-007: Async wrapping of sync search calls at server call sites.

Verifies that synchronous SearchEngine methods wrapped in asyncio.to_thread()
do not block the asyncio event loop, allowing concurrent operations.
"""

import ast
import asyncio
import inspect
import time
from pathlib import Path
from types import ModuleType

import pytest


def _guarded_sources(*extra_modules: ModuleType) -> dict[str, str]:
    """Sources the static offload guards scan: server.py, every module under
    lithos/tools/ (handlers move there extraction PR by extraction PR), plus
    any explicitly passed modules."""
    import lithos.server
    import lithos.tools

    sources = {"lithos/server.py": Path(inspect.getfile(lithos.server)).read_text()}
    tools_dir = Path(inspect.getfile(lithos.tools)).parent
    for module_file in sorted(tools_dir.glob("*.py")):
        sources[f"lithos/tools/{module_file.name}"] = module_file.read_text()
    for module in extra_modules:
        path = Path(inspect.getfile(module))
        sources[f"lithos/{path.name}"] = path.read_text()
    return sources


def _is_search_method_ref(node: ast.expr, methods: frozenset[str]) -> bool:
    """True for attribute chains ending ``.search.<method>`` or ``._search.<method>``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr in methods
        and isinstance(node.value, ast.Attribute)
        and node.value.attr in ("search", "_search")
    )


def _scan_search_call_sites(
    sources: dict[str, str], methods: frozenset[str]
) -> tuple[list[str], int]:
    """Return (direct-call violations, count of to_thread-offloaded references)."""
    violations: list[str] = []
    offloaded = 0
    for origin, source in sources.items():
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            if _is_search_method_ref(node.func, methods):
                violations.append(f"{origin}:{node.lineno} {ast.unparse(node.func)}(...)")
            func = node.func
            is_to_thread = isinstance(func, ast.Attribute) and func.attr == "to_thread"
            if is_to_thread and node.args and _is_search_method_ref(node.args[0], methods):
                offloaded += 1
    return violations, offloaded


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
        """Search read-path call sites are offloaded via asyncio.to_thread.

        Static AST scan of lithos/server.py plus every lithos/tools/ module:
        a search method invoked directly is a violation (it would block the
        event loop on Tantivy/Chroma); the legitimate pattern passes the
        bound method to asyncio.to_thread. The count floor proves the scan
        actually saw the call sites (4 modes in lithos_search + the
        content_query path in lithos_list).
        """
        read_methods = frozenset(
            {"full_text_search", "semantic_search", "hybrid_search", "graph_search"}
        )
        violations, offloaded = _scan_search_call_sites(_guarded_sources(), read_methods)
        assert not violations, f"direct (blocking) search calls: {violations}"
        assert offloaded >= 5, f"scan looks broken: only {offloaded} offloaded call sites found"

    @pytest.mark.asyncio
    async def test_server_search_mutation_sites_use_to_thread(self) -> None:
        """Regression guard for #199: the write and file-watcher paths used
        to call ``search.index()`` synchronously, blocking the event loop
        for Tantivy commits and ChromaDB embeddings.

        The mutation call sites live behind Corpus intake now
        (``intake.py`` / ``watch_intake.py`` as ``self._search.index`` /
        ``.remove``), so those modules are scanned alongside server.py and
        the tool modules. Any direct call is a violation; the count floor
        proves the scan still sees the offloaded intake sites.
        """
        import lithos.intake
        import lithos.watch_intake

        mutation_methods = frozenset({"index", "remove"})
        violations, offloaded = _scan_search_call_sites(
            _guarded_sources(lithos.intake, lithos.watch_intake), mutation_methods
        )
        assert not violations, f"direct (blocking) search mutations (#199): {violations}"
        assert offloaded >= 4, f"scan looks broken: only {offloaded} offloaded call sites found"
