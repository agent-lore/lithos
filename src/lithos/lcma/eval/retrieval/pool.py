"""Run a query set through every method against one snapshot and pool the results.

Methods are run interleaved (a different first method per query) so that
cache warmth and host load fall evenly across them, and each call is timed
on its own. The retrieve callable must be the facade's ``retrieve`` with
``record=False`` bound: pooling must not write receipts or coactivation into
the snapshot it reads.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from lithos.knowledge import KnowledgeManager
from lithos.lcma.eval.retrieval.cases import (
    METHODS,
    MethodRun,
    Query,
    Ranked,
    RetrievalCase,
)
from lithos.search import SearchEngine

_VECTOR_SIMILARITY = re.compile(r"semantic similarity (\d+(?:\.\d+)?)")


class Retriever(Protocol):
    def __call__(
        self,
        query: str,
        *,
        limit: int,
        namespace_filter: list[str] | None,
        tags: list[str] | None,
        path_prefix: str | None,
    ) -> Awaitable[dict[str, object]]: ...


def vector_similarity(reasons: Sequence[object]) -> float | None:
    """The vector scout's similarity, as ``lithos_retrieve`` states it in ``reasons``."""
    for reason in reasons:
        match = _VECTOR_SIMILARITY.search(str(reason))
        if match:
            return float(match.group(1))
    return None


def _ranked(results: Sequence[Any], *, score_attr: str) -> tuple[Ranked, ...]:
    return tuple(
        Ranked(id=r.id, rank=i, score=float(getattr(r, score_attr)))
        for i, r in enumerate(results, start=1)
    )


def _ranked_retrieve(results: Sequence[dict[str, Any]]) -> tuple[Ranked, ...]:
    return tuple(
        Ranked(
            id=str(r["id"]),
            rank=i,
            score=float(r["score"]),
            scouts=tuple(str(s) for s in r.get("scouts") or ()),
            vector_similarity=vector_similarity(r.get("reasons") or ()),
        )
        for i, r in enumerate(results, start=1)
    )


async def _timed(call: Callable[[], Awaitable[tuple[Ranked, ...]]]) -> MethodRun:
    t0 = time.perf_counter()
    results = await call()
    return MethodRun(results=results, latency_ms=(time.perf_counter() - t0) * 1000)


async def run_methods(
    query: Query,
    *,
    search: SearchEngine,
    knowledge: KnowledgeManager,
    retrieve: Retriever,
    depth: int,
    first: int = 0,
) -> dict[str, MethodRun]:
    """Every method on one query at *depth*, starting from METHODS[first]."""
    scope = knowledge.scope_ids(
        path_prefix=query.path_prefix,
        namespaces=query.namespace_filter,
        tags=query.tags,
    )
    empty_scope = scope is not None and not scope

    async def hybrid() -> tuple[Ranked, ...]:
        if empty_scope:
            return ()
        rows = await asyncio.to_thread(
            search.hybrid_search, query=query.query, limit=depth, within_ids=scope
        )
        return _ranked(rows, score_attr="score")

    async def semantic() -> tuple[Ranked, ...]:
        if empty_scope:
            return ()
        rows = await asyncio.to_thread(
            search.semantic_search,
            query=query.query,
            limit=depth,
            threshold=0.0,
            within_ids=scope,
        )
        return _ranked(rows, score_attr="similarity")

    async def retrieve_() -> tuple[Ranked, ...]:
        envelope = await retrieve(
            query.query,
            limit=depth,
            namespace_filter=query.namespace_filter,
            tags=query.tags,
            path_prefix=query.path_prefix,
        )
        results = envelope.get("results")
        return _ranked_retrieve(list(results) if isinstance(results, list) else [])

    calls = {"hybrid": hybrid, "semantic": semantic, "retrieve": retrieve_}
    order = [*METHODS[first % len(METHODS) :], *METHODS[: first % len(METHODS)]]
    runs: dict[str, MethodRun] = {}
    for name in order:
        runs[name] = await _timed(calls[name])
    return {name: runs[name] for name in METHODS}


async def note_views(
    ids: Sequence[str], *, knowledge: KnowledgeManager, text_chars: int
) -> dict[str, dict[str, Any]]:
    """Title, path, namespace and a text head per pooled note, for the labeller."""
    views: dict[str, dict[str, Any]] = {}
    for note_id in ids:
        try:
            doc, _ = await knowledge.read(id=note_id)
        except FileNotFoundError:
            views[note_id] = {"title": None, "path": None, "namespace": None, "text": None}
            continue
        text = " ".join(doc.content.split())
        views[note_id] = {
            "title": doc.title,
            "path": str(doc.path),
            "namespace": doc.metadata.namespace,
            "text": text if len(text) <= text_chars else text[: text_chars - 1] + "…",
        }
    return views


def snapshot_info(*, data_dir: str, note_count: int, lithos_version: str) -> dict[str, Any]:
    return {
        "data_dir": data_dir,
        "note_count": note_count,
        "lithos_version": lithos_version,
        "pooled_at": datetime.now(UTC).isoformat(),
    }


async def pool_queries(
    queries: Sequence[Query],
    *,
    search: SearchEngine,
    knowledge: KnowledgeManager,
    retrieve: Retriever,
    snapshot: dict[str, Any],
    depth: int = 10,
    text_chars: int = 600,
    warmup: bool = True,
    progress: Callable[[RetrievalCase], None] | None = None,
) -> list[RetrievalCase]:
    """Pool every query; with *warmup*, run the first query once untimed first.

    The first call to each backend pays its cold start (Tantivy reader,
    Chroma collection, embedding model). Without the warm-up that cost lands
    on whichever method the interleaving puts first and skews its latency.
    """
    cases: list[RetrievalCase] = []
    if warmup and queries:
        await run_methods(
            queries[0], search=search, knowledge=knowledge, retrieve=retrieve, depth=depth
        )
    for i, query in enumerate(queries):
        methods = await run_methods(
            query, search=search, knowledge=knowledge, retrieve=retrieve, depth=depth, first=i
        )
        pooled: list[str] = []
        for name in METHODS:
            pooled.extend(r.id for r in methods[name].results if r.id not in pooled)
        case = RetrievalCase(
            case_id=query.query_id,
            query=query.query,
            caller=query.caller,
            slice=query.slice,
            scope=dict(query.scope),
            depth=depth,
            methods=methods,
            notes=await note_views(pooled, knowledge=knowledge, text_chars=text_chars),
            snapshot=snapshot,
        )
        cases.append(case)
        if progress is not None:
            progress(case)
    return cases
