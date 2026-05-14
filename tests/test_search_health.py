"""Tests for the SearchEngine public health/counts surface (#224)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lithos.config import LithosConfig
from lithos.search import ChromaIndex, Healthy, SearchEngine, Unhealthy


@pytest.mark.asyncio
async def test_health_returns_healthy_when_both_backends_respond(
    search_engine: SearchEngine,
) -> None:
    """Healthy is returned when Tantivy and Chroma are both reachable."""
    status = search_engine.health()
    assert isinstance(status, Healthy)


@pytest.mark.asyncio
async def test_health_returns_unhealthy_when_semantic_store_corrupt(
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
    test_config: LithosConfig,
) -> None:
    """Health probes the embedding model; a load failure surfaces as Unhealthy."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("model probe failed")

    # Inject a real semantic backend whose health_check raises, exercising
    # the engine's health-composition behavior without reaching into
    # engine-private state (issue #264).
    semantic = ChromaIndex(
        test_config.storage.chroma_path,
        test_config.search.embedding_model,
        device=test_config.search.device,
    )
    semantic.health_check = _boom  # type: ignore[method-assign]
    engine = await SearchEngine.create(test_config, semantic_backend=semantic)

    status = engine.health()

    assert isinstance(status, Unhealthy)
    assert "embedding model" in status.reason
    assert "model probe failed" in status.reason


@pytest.mark.asyncio
async def test_count_documents_matches_backend(search_engine: SearchEngine) -> None:
    """count_documents() agrees with the underlying full-text backend count."""
    # Empty engine — no docs indexed.
    assert search_engine.count_documents() == 0


@pytest.mark.asyncio
async def test_count_chunks_matches_backend(search_engine: SearchEngine) -> None:
    """count_chunks() agrees with the underlying semantic backend count."""
    assert search_engine.count_chunks() == 0


@pytest.mark.asyncio
async def test_count_chunks_returns_zero_when_semantic_unhealthy(
    search_engine: SearchEngine,
) -> None:
    """count_chunks() returns 0 (not raise) when the semantic store is unhealthy."""
    search_engine._semantic_store_checked = True
    search_engine._semantic_store_healthy = False
    search_engine._semantic_store_error = "quarantined"

    assert search_engine.count_chunks() == 0


@pytest.mark.asyncio
async def test_needs_initial_rebuild_reflects_ft_state(
    test_config: LithosConfig,
) -> None:
    """needs_initial_rebuild() is True for a fresh engine (newly-created index)."""
    engine = await SearchEngine.create(test_config)
    # A freshly-created index has no schema marker on disk before create runs,
    # so open_or_create writes one and flips the rebuild flag.
    assert engine.needs_initial_rebuild() is True


@pytest.mark.asyncio
async def test_semantic_backend_health_check_does_not_call_encode(
    test_config: LithosConfig,
) -> None:
    """Regression for #198: liveness probe must not invoke the embedding model.

    The previous implementation called ``self.model.encode(["health check"])`` on
    every HTTP /health hit, which Docker HEALTHCHECKs and load-balancer liveness
    probes call every few seconds.

    Tests the ``ChromaIndex.health_check`` contract directly — analogous to
    the ``TantivyIndex`` direct-instantiation pattern in
    ``test_freshness_conformance.py`` (issue #264).
    """
    idx = ChromaIndex(
        test_config.storage.chroma_path,
        test_config.search.embedding_model,
        device=test_config.search.device,
    )
    await idx.ensure_model_loaded()

    with patch.object(idx.model, "encode") as encode_spy:
        idx.health_check()
    encode_spy.assert_not_called()


@pytest.mark.asyncio
async def test_semantic_backend_health_check_raises_when_model_unloaded(
    test_config: LithosConfig,
) -> None:
    """Liveness probe still fails loudly when the model is genuinely missing."""
    idx = ChromaIndex(
        test_config.storage.chroma_path,
        test_config.search.embedding_model,
        device=test_config.search.device,
    )
    # No ``ensure_model_loaded`` — the model attribute stays unset, simulating
    # the production failure mode the probe must catch.
    with pytest.raises(RuntimeError, match="not loaded"):
        idx.health_check()


@pytest.mark.asyncio
async def test_is_semantic_model_loaded_reflects_create_contract(
    test_config: LithosConfig,
) -> None:
    """``SearchEngine.create`` eagerly loads the embedding model.

    This is the public observability point for the eager-load contract
    introduced in #224 — operators / ops dashboards can query the
    engine's readiness without reaching into backend internals (issue #264).
    """
    engine = await SearchEngine.create(test_config)
    assert engine.is_semantic_model_loaded() is True
