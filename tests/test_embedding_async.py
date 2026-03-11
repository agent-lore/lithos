"""Tests for async embedding model loading in ChromaIndex."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from lithos.search import ChromaIndex


@pytest.mark.asyncio
async def test_ensure_model_loaded_is_nonblocking(tmp_path):
    """ensure_model_loaded() is idempotent and only loads the model once under concurrency."""
    load_count = 0

    def slow_constructor(model_name):
        nonlocal load_count
        time.sleep(0.1)
        load_count += 1
        return MagicMock()

    chroma = ChromaIndex(tmp_path / "chroma")

    with patch("lithos.search.SentenceTransformer", side_effect=slow_constructor):
        await asyncio.gather(
            chroma.ensure_model_loaded(),
            chroma.ensure_model_loaded(),
        )

    assert load_count == 1, f"Model loaded {load_count} times, expected 1"
    assert chroma._model is not None
