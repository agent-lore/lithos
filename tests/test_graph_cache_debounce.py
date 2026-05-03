"""Tests for KnowledgeGraph save_cache debounce (#203).

Bursts of writes used to serialise the entire graph on every mutation.
``add_document`` / ``remove_document`` / ``clear`` now mark the graph dirty
and only flush when N ops or K seconds have elapsed; explicit flush points
continue to call :meth:`save_cache` directly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from lithos.config import LithosConfig
from lithos.graph import KnowledgeGraph
from lithos.knowledge import KnowledgeDocument, KnowledgeMetadata


def _make_doc(doc_id: str) -> KnowledgeDocument:
    return KnowledgeDocument(
        id=doc_id,
        title=f"Doc {doc_id}",
        content="body",
        path=Path(f"notes/{doc_id}.md"),
        metadata=KnowledgeMetadata(
            id=doc_id,
            title=f"Doc {doc_id}",
            author="agent",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    )


def test_add_document_does_not_flush_on_single_call(test_config: LithosConfig) -> None:
    """A single add does not trigger a flush — would be wasteful per-op IO."""
    graph = KnowledgeGraph(test_config)
    with patch.object(graph, "save_cache", wraps=graph.save_cache) as save_spy:
        graph.add_document(_make_doc("11111111-1111-1111-1111-111111111111"))
    save_spy.assert_not_called()


def test_burst_of_writes_under_threshold_does_not_flush(test_config: LithosConfig) -> None:
    """A burst of mutations below the op threshold flushes zero times."""
    graph = KnowledgeGraph(test_config)
    with patch.object(graph, "save_cache", wraps=graph.save_cache) as save_spy:
        for i in range(5):
            graph.add_document(_make_doc(f"00000000-0000-0000-0000-00000000000{i}"))
    save_spy.assert_not_called()


def test_op_threshold_triggers_one_flush(test_config: LithosConfig) -> None:
    """When the op threshold is crossed, exactly one flush fires."""
    graph = KnowledgeGraph(test_config)
    graph._FLUSH_AFTER_OPS = 3  # narrow the threshold for this test
    with patch.object(graph, "save_cache", wraps=graph.save_cache) as save_spy:
        for i in range(3):
            graph.add_document(_make_doc(f"00000000-0000-0000-0000-00000000000{i}"))
    save_spy.assert_called_once()


def test_seconds_threshold_triggers_flush(test_config: LithosConfig) -> None:
    """An idle window past the seconds threshold flushes on the next mutation."""
    graph = KnowledgeGraph(test_config)
    with patch.object(graph, "save_cache", wraps=graph.save_cache) as save_spy:
        graph.add_document(_make_doc("11111111-1111-1111-1111-111111111111"))
        # Simulate K seconds elapsed since the last flush.
        graph._last_flush_at -= graph._FLUSH_AFTER_SECONDS + 0.1
        graph.add_document(_make_doc("22222222-2222-2222-2222-222222222222"))
    assert save_spy.call_count == 1


def test_save_cache_resets_debounce_state(test_config: LithosConfig) -> None:
    """An explicit flush resets dirty count and last-flush timestamp."""
    graph = KnowledgeGraph(test_config)
    graph.add_document(_make_doc("11111111-1111-1111-1111-111111111111"))
    assert graph._dirty_ops == 1
    graph.save_cache()
    assert graph._dirty_ops == 0


def test_remove_document_marks_dirty(test_config: LithosConfig) -> None:
    """remove_document increments the dirty counter (and may trigger flush)."""
    graph = KnowledgeGraph(test_config)
    graph.add_document(_make_doc("11111111-1111-1111-1111-111111111111"))
    pre = graph._dirty_ops
    graph.remove_document("11111111-1111-1111-1111-111111111111")
    assert graph._dirty_ops == pre + 1


def test_clear_resets_debounce_state(test_config: LithosConfig) -> None:
    """clear() drops in-memory state and the dirty counter (nothing left to flush)."""
    graph = KnowledgeGraph(test_config)
    graph.add_document(_make_doc("11111111-1111-1111-1111-111111111111"))
    assert graph._dirty_ops == 1
    graph.clear()
    assert graph._dirty_ops == 0


@pytest.mark.asyncio
async def test_explicit_save_cache_still_flushes(test_config: LithosConfig) -> None:
    """save_cache() is the explicit flush API — still works on demand."""
    graph = KnowledgeGraph(test_config)
    graph.add_document(_make_doc("11111111-1111-1111-1111-111111111111"))
    cache_path = graph.graph_cache_path
    assert not cache_path.exists()
    graph.save_cache()
    assert cache_path.exists()
