"""Tests for the SearchEngine.create() async factory."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lithos.config import LithosConfig
from lithos.search import SearchEngine


@pytest.mark.asyncio
async def test_create_loads_embedding_model_eagerly(test_config: LithosConfig) -> None:
    """create() returns an engine with the embedding model already loaded."""
    with patch("lithos.search.SentenceTransformer", return_value=MagicMock()) as ctor:
        engine = await SearchEngine.create(test_config)

    assert engine.chroma._model is not None, "embedding model should be loaded by create()"
    ctor.assert_called_once()


@pytest.mark.asyncio
async def test_create_propagates_model_load_failure(test_config: LithosConfig) -> None:
    """create() surfaces an exception raised while loading the embedding model."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("model load failed")

    with (
        patch("lithos.search.SentenceTransformer", side_effect=_boom),
        pytest.raises(RuntimeError, match="model load failed"),
    ):
        await SearchEngine.create(test_config)


@pytest.mark.asyncio
async def test_create_quarantines_corrupt_chroma_store(test_config: LithosConfig) -> None:
    """create() quarantines an unreadable Chroma store and proceeds with a clean one."""
    chroma_path: Path = test_config.storage.chroma_path
    chroma_path.mkdir(parents=True, exist_ok=True)
    sentinel_file = chroma_path / "broken.sqlite3"
    sentinel_file.write_bytes(b"not a real sqlite db")

    # First probe fails (corrupt), second probe (after quarantine) succeeds.
    probe_responses = iter([(False, "broken store"), (True, None)])

    def _fake_probe(self, timeout_seconds: float = 10.0):
        return next(probe_responses)

    with (
        patch("lithos.search.SentenceTransformer", return_value=MagicMock()),
        patch("lithos.search.ChromaIndex.probe_store", _fake_probe),
    ):
        engine = await SearchEngine.create(test_config)

    # The original store directory should be replaced by a fresh one and a
    # quarantined backup directory should now sit alongside it.
    siblings = list(chroma_path.parent.iterdir())
    backup_dirs = [p for p in siblings if p.name.startswith(f"{chroma_path.name}.corrupt-")]
    assert backup_dirs, "expected a quarantined backup directory after corrupt probe"
    assert chroma_path.exists(), "fresh chroma path should exist after quarantine"
    assert engine.chroma._model is not None
