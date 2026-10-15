"""Replayable LLM call log — eval suite E1 (``docs/plans/llm-eval-suite.md``).

Every background adjudication call is recorded verbatim — the exact messages
sent and the raw completion — so a past judgement can be replayed against a
new model or prompt. Rows live in ``stats.db`` (``llm_call_log``, owned by
:class:`lithos.lcma.stats.StatsStore`); the two payloads per call are
content-addressed blobs under ``<lithos_store>/llm-calls/blobs/<sha256>`` so a
later note edit cannot change what a past call saw and identical payloads are
stored once. Hashes alone would not do: once a note changes they cannot
reconstruct the input (review of PR #431).

Recording is best-effort and never raises into the inference engine. The
daily enrich sweep drops rows past the retention window and then any blob no
remaining row references.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lithos.lcma.stats import StatsStore

logger = logging.getLogger(__name__)

#: Every outcome the engine can log. ``partial`` is a usable completion that
#: violated the response contract somewhere; ``stale`` means the note changed
#: mid-call and the judgements were discarded.
CALL_OUTCOMES = frozenset({"ok", "partial", "parse_error", "error", "stale"})

#: Outcomes whose completion is a usable reference judgement for fixtures.
REPLAYABLE_OUTCOMES = frozenset({"ok", "partial"})

#: Where the blobs live relative to the ``.lithos`` store directory.
BLOB_SUBDIR = Path("llm-calls") / "blobs"

_SHA256_NAME = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class LlmCall:
    """One adjudication call exactly as the engine saw it, ready to be logged.

    ``request`` is the structured payload the engine built (verbatim messages
    plus the focus/candidate inputs they were rendered from, see
    :func:`lithos.lcma.edge_inference.request_payload`); it is hashed and
    stored whole, so an export can both replay the exact prompt and rebuild
    a new prompt version from the same inputs.
    """

    node_id: str
    doc_version: int
    inference_version: int
    model: str
    reasoning_effort: str | None
    request: dict[str, Any]
    completion: str | None
    outcome: str
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int | None
    estimated: bool
    latency_ms: float
    edges_written: int = 0
    error: str | None = None
    shadow: bool = False

    def __post_init__(self) -> None:
        if self.outcome not in CALL_OUTCOMES:
            raise ValueError(f"unknown LLM call outcome {self.outcome!r}")

    def as_row(self, *, request_sha: str, completion_sha: str | None) -> dict[str, Any]:
        """Column values for ``StatsStore.record_llm_call`` (blobs already stored)."""
        candidates = [
            {"node_id": c.get("node_id"), "similarity": c.get("similarity")}
            for c in self.request.get("candidates", [])
            if isinstance(c, dict)
        ]
        return {
            "called_at": datetime.now(UTC).isoformat(),
            "node_id": self.node_id,
            "doc_version": self.doc_version,
            "inference_version": self.inference_version,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "request_sha": request_sha,
            "completion_sha": completion_sha,
            "candidates_json": json.dumps(candidates),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "estimated": int(self.estimated),
            "latency_ms": self.latency_ms,
            "outcome": self.outcome,
            "edges_written": self.edges_written,
            "shadow": int(self.shadow),
            "error": self.error,
        }


def encode_request(request: dict[str, Any]) -> bytes:
    """Canonical bytes for a request payload: sorted keys, UTF-8, no ASCII escapes."""
    return json.dumps(request, sort_keys=True, ensure_ascii=False).encode("utf-8")


class BlobStore:
    """Content-addressed, write-once file store keyed by SHA-256 of the bytes."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, sha: str) -> Path:
        return self._root / sha

    def put(self, data: bytes) -> str:
        """Store *data* if absent and return its hex digest.

        Written to a temp file in the same directory and renamed into place,
        so a reader never sees a partial blob and a crash leaves at most a
        ``.tmp-*`` file the sweep ignores.
        """
        sha = hashlib.sha256(data).hexdigest()
        target = self.path_for(sha)
        if target.exists():
            return sha
        self._root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._root, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return sha

    def get(self, sha: str) -> bytes | None:
        try:
            return self.path_for(sha).read_bytes()
        except FileNotFoundError:
            return None

    def sweep(self, keep: set[str]) -> int:
        """Delete every blob whose digest is not in *keep*; return the count."""
        if not self._root.is_dir():
            return 0
        removed = 0
        for path in self._root.iterdir():
            if not _SHA256_NAME.match(path.name) or path.name in keep:
                continue
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
                removed += 1
        return removed

    def usage(self) -> tuple[int, int]:
        """``(blob_count, total_bytes)`` — the retention-window sizing numbers."""
        if not self._root.is_dir():
            return 0, 0
        count = total = 0
        for path in self._root.iterdir():
            if _SHA256_NAME.match(path.name):
                count += 1
                total += path.stat().st_size
        return count, total


class CallLogWriter:
    """Record calls (blobs + row) and sweep the log on behalf of the inference engine."""

    def __init__(self, stats_store: StatsStore, blob_root: Path) -> None:
        self._store = stats_store
        self._blobs = BlobStore(blob_root)

    @classmethod
    def for_store(cls, stats_store: StatsStore) -> CallLogWriter:
        """Writer whose blobs sit beside the store's database (``.lithos/llm-calls/blobs``)."""
        return cls(stats_store, stats_store.config.storage.lithos_store_path / BLOB_SUBDIR)

    @property
    def blobs(self) -> BlobStore:
        return self._blobs

    async def record(self, call: LlmCall) -> None:
        """Persist *call*; failures are logged and swallowed (the log is additive)."""
        try:
            request_sha = await asyncio.to_thread(self._blobs.put, encode_request(call.request))
            completion_sha: str | None = None
            if call.completion is not None:
                completion_sha = await asyncio.to_thread(
                    self._blobs.put, call.completion.encode("utf-8")
                )
            await self._store.record_llm_call(
                call.as_row(request_sha=request_sha, completion_sha=completion_sha)
            )
        except Exception:
            logger.exception("LLM call log write failed for %s; call not recorded", call.node_id)

    async def sweep(self, retention_days: int, *, now: datetime | None = None) -> tuple[int, int]:
        """Drop rows older than the window, then orphaned blobs. Returns ``(rows, blobs)``."""
        cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
        rows = await self._store.delete_llm_calls_before(cutoff)
        keep = await self._store.referenced_llm_blob_shas()
        blobs = await asyncio.to_thread(self._blobs.sweep, keep)
        return rows, blobs
