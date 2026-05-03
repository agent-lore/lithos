"""Tests for the SearchEngine public health/counts surface (#224)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lithos.config import LithosConfig
from lithos.search import Healthy, SearchEngine, Unhealthy


@pytest.mark.asyncio
async def test_health_returns_healthy_when_both_backends_respond(
    search_engine: SearchEngine,
) -> None:
    """Healthy is returned when Tantivy and Chroma are both reachable."""
    status = search_engine.health()
    assert isinstance(status, Healthy)


@pytest.mark.asyncio
async def test_health_returns_unhealthy_when_chroma_store_corrupt(
    search_engine: SearchEngine,
) -> None:
    """Health composes the Chroma probe; a corrupt store surfaces as Unhealthy."""
    # SearchEngine.create() already primed the probe cache; reset so this
    # test's failure path is the one health() observes.
    search_engine._semantic_store_checked = True
    search_engine._semantic_store_healthy = False
    search_engine._semantic_store_error = "simulated corruption"

    status = search_engine.health()
    assert isinstance(status, Unhealthy)
    assert "chroma" in status.reason
    assert "simulated corruption" in status.reason


@pytest.mark.asyncio
async def test_health_returns_unhealthy_when_embedding_model_load_fails(
    search_engine: SearchEngine,
) -> None:
    """Health probes the embedding model; a load failure surfaces as Unhealthy."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("model probe failed")

    with patch.object(search_engine._chroma, "health_check", side_effect=_boom):
        status = search_engine.health()

    assert isinstance(status, Unhealthy)
    assert "embedding model" in status.reason
    assert "model probe failed" in status.reason


@pytest.mark.asyncio
async def test_count_documents_matches_backend(search_engine: SearchEngine) -> None:
    """count_documents() agrees with the underlying full-text backend count."""
    # Empty engine — no docs indexed.
    assert search_engine.count_documents() == search_engine._tantivy.count_docs() == 0


@pytest.mark.asyncio
async def test_count_chunks_matches_backend(search_engine: SearchEngine) -> None:
    """count_chunks() agrees with the underlying semantic backend count."""
    assert search_engine.count_chunks() == 0


@pytest.mark.asyncio
async def test_count_chunks_returns_zero_when_chroma_unhealthy(
    search_engine: SearchEngine,
) -> None:
    """count_chunks() returns 0 (not raise) when the semantic store is unhealthy."""
    search_engine._semantic_store_checked = True
    search_engine._semantic_store_healthy = False
    search_engine._semantic_store_error = "quarantined"

    assert search_engine.count_chunks() == 0


@pytest.mark.asyncio
async def test_needs_initial_rebuild_reflects_tantivy_state(
    test_config: LithosConfig,
) -> None:
    """needs_initial_rebuild() is True for a fresh engine (newly-created index)."""
    engine = await SearchEngine.create(test_config)
    # A freshly-created index has no schema marker on disk before create runs,
    # so open_or_create writes one and flips the rebuild flag.
    assert engine.needs_initial_rebuild() is True
