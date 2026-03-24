"""Reconcile - drift detection and repair for derived state projections.

Core internal logic only.  Not exposed as an MCP tool.

Scope behaviour:
    indices              — repairs Tantivy and ChromaDB from the markdown corpus
    graph                — repairs the wiki-link graph cache
    provenance_projection— repairs projected LCMA edges (returns supported=False
                          before edges.db / LCMA storage exists)
    all                  — runs the three scopes in that order and aggregates

Markdown/frontmatter is the source of truth and is NEVER mutated here.
Only derived state (indices, graph cache, provenance edges) may be repaired.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from lithos.config import LithosConfig, get_config
from lithos.graph import KnowledgeGraph
from lithos.knowledge import KnowledgeDocument, KnowledgeManager, parse_wiki_links
from lithos.search import SearchEngine
from lithos.telemetry import get_tracer, lithos_metrics

logger = logging.getLogger(__name__)

ReconcileScope = Literal["all", "indices", "graph", "provenance_projection"]
ReconcileStatus = Literal["ok", "noop", "partial_failure", "failed"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_result(
    scope: str,
    dry_run: bool,
    supported: bool = True,
    status: ReconcileStatus = "ok",
    scanned: int = 0,
    repaired: int = 0,
    failed: int = 0,
    skipped: int = 0,
    actions: list[dict[str, Any]] | None = None,
    failures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a structured reconcile result dict."""
    return {
        "scope": scope,
        "dry_run": dry_run,
        "supported": supported,
        "status": status,
        "summary": {
            "scanned": scanned,
            "repaired": repaired,
            "failed": failed,
            "skipped": skipped,
        },
        "actions": actions or [],
        "failures": failures or [],
    }


async def _scan_corpus(config: LithosConfig) -> list[KnowledgeDocument]:
    """Return all documents from the authoritative markdown corpus.

    Passes ``config`` explicitly into KnowledgeManager so that callers
    using a non-global config (e.g. tests) get the correct knowledge path.
    """
    knowledge = KnowledgeManager(config=config)
    _, total = await knowledge.list_all(limit=0)
    if total == 0:
        return []
    docs, _ = await knowledge.list_all(limit=total)
    return docs


def _aggregate_status(statuses: list[ReconcileStatus]) -> ReconcileStatus:
    """Compute aggregate status from a list of per-scope statuses."""
    failures = [s for s in statuses if s in ("failed", "partial_failure")]
    any_ok = any(s == "ok" for s in statuses)

    if not failures:
        return "ok" if any_ok else "noop"
    if len(failures) < len(statuses):
        return "partial_failure"
    return "failed"


# ---------------------------------------------------------------------------
# Per-scope reconcile functions
# ---------------------------------------------------------------------------


async def _reconcile_indices(config: LithosConfig, dry_run: bool) -> dict[str, Any]:
    """Reconcile Tantivy and ChromaDB indices against the markdown corpus."""
    tracer = get_tracer()
    actions: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    with tracer.start_as_current_span("lithos.reconcile.scan") as scan_span:
        scan_span.set_attribute("lithos.reconcile.scope", "indices")
        try:
            corpus_docs = await _scan_corpus(config)
        except Exception as exc:
            logger.error("Failed to scan corpus for indices reconcile: %s", exc)
            return _make_result(
                "indices",
                dry_run,
                status="failed",
                failed=1,
                failures=[{"code": "internal_error", "detail": str(exc)}],
            )
        corpus_ids = {doc.id for doc in corpus_docs}

    search = SearchEngine(config)
    with tracer.start_as_current_span("lithos.reconcile.diff") as diff_span:
        diff_span.set_attribute("lithos.reconcile.scope", "indices")

        # --- Tantivy drift detection ---
        try:
            if search.tantivy.needs_rebuild:
                actions.append(
                    {"backend": "tantivy", "action": "full_rebuild", "reason": "schema_mismatch"}
                )
            else:
                tantivy_ids = search.tantivy.get_indexed_doc_ids()
                if tantivy_ids != corpus_ids:
                    actions.append(
                        {
                            "backend": "tantivy",
                            "action": "full_rebuild",
                            "reason": "doc_set_mismatch",
                        }
                    )
        except Exception as exc:
            logger.warning("Tantivy drift check failed: %s", exc)
            actions.append(
                {"backend": "tantivy", "action": "full_rebuild", "reason": "check_failed"}
            )

        # --- ChromaDB drift detection ---
        try:
            chroma_doc_ids = search.chroma.get_indexed_doc_ids()
            if chroma_doc_ids != corpus_ids:
                actions.append(
                    {"backend": "chroma", "action": "full_rebuild", "reason": "doc_set_mismatch"}
                )
        except Exception as exc:
            logger.warning("ChromaDB drift check failed: %s", exc)
            actions.append(
                {"backend": "chroma", "action": "full_rebuild", "reason": "check_failed"}
            )

    if not actions:
        return _make_result("indices", dry_run, status="noop", scanned=len(corpus_docs))

    if dry_run:
        return _make_result(
            "indices",
            dry_run,
            status="ok",
            scanned=len(corpus_docs),
            repaired=len(actions),
            actions=actions,
        )

    # Apply repairs
    repaired = 0
    with tracer.start_as_current_span("lithos.reconcile.apply") as apply_span:
        apply_span.set_attribute("lithos.reconcile.scope", "indices")
        for action in actions:
            backend = action["backend"]
            apply_span.set_attribute("lithos.reconcile.backend", backend)
            try:
                if backend == "tantivy":
                    search.tantivy.rebuild_from_docs(corpus_docs)
                    repaired += 1
                elif backend == "chroma":
                    search.chroma.clear()
                    for doc in corpus_docs:
                        search.chroma.add_document(doc)
                    repaired += 1
            except Exception as exc:
                logger.error("Failed to repair %s backend: %s", backend, exc)
                failures.append(
                    {"code": "index_rebuild_failed", "backend": backend, "detail": str(exc)}
                )

    n_failed = len(failures)
    if n_failed == 0:
        status: ReconcileStatus = "ok"
    elif repaired == 0:
        status = "failed"
    else:
        status = "partial_failure"

    lithos_metrics.reconcile_ops.add(1, {"scope": "indices", "status": status})
    return _make_result(
        "indices",
        dry_run,
        status=status,
        scanned=len(corpus_docs),
        repaired=repaired,
        failed=n_failed,
        actions=actions,
        failures=failures,
    )


async def _reconcile_graph(config: LithosConfig, dry_run: bool) -> dict[str, Any]:
    """Reconcile the wiki-link graph cache against the markdown corpus.

    When the cache is already consistent (no node/edge drift), a second pass
    scans for stale wiki-links (targets that don't resolve to any document)
    and reports them as ``stale_link`` actions.  Stale-link detection is
    skipped when the cache itself needs a rebuild; the caller must reconcile
    again after repair to surface stale links.
    """
    tracer = get_tracer()
    actions: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    with tracer.start_as_current_span("lithos.reconcile.scan") as scan_span:
        scan_span.set_attribute("lithos.reconcile.scope", "graph")
        try:
            corpus_docs = await _scan_corpus(config)
        except Exception as exc:
            logger.error("Failed to scan corpus for graph reconcile: %s", exc)
            return _make_result(
                "graph",
                dry_run,
                status="failed",
                failed=1,
                failures=[{"code": "internal_error", "detail": str(exc)}],
            )

    graph = KnowledgeGraph(config)
    with tracer.start_as_current_span("lithos.reconcile.diff") as diff_span:
        diff_span.set_attribute("lithos.reconcile.scope", "graph")
        corpus_ids = {doc.id for doc in corpus_docs}
        cache_path = graph.graph_cache_path

        if not cache_path.exists():
            actions.append(
                {"target": "graph_cache", "action": "full_rebuild", "reason": "cache_missing"}
            )
        else:
            loaded = graph.load_cache()
            if not loaded:
                actions.append(
                    {
                        "target": "graph_cache",
                        "action": "full_rebuild",
                        "reason": "cache_unreadable",
                    }
                )
            else:
                cached_ids = graph.get_doc_ids()
                if cached_ids != corpus_ids:
                    actions.append(
                        {
                            "target": "graph_cache",
                            "action": "full_rebuild",
                            "reason": "node_set_mismatch",
                            "corpus_count": len(corpus_ids),
                            "cached_count": len(cached_ids),
                        }
                    )
                else:
                    # Node set matches — check for edge/link-set drift.
                    # Compare raw (source_id, link_target_text) pairs so we
                    # don't need to re-resolve links through the lookup tables.
                    corpus_links: set[tuple[str, str]] = set()
                    for doc in corpus_docs:
                        for link in parse_wiki_links(doc.content):
                            corpus_links.add((doc.id, link.target))

                    cached_links: set[tuple[str, str]] = set()
                    for source, _target, data in graph.graph.edges(data=True):
                        link_text = data.get("link_text", "")
                        if link_text:
                            cached_links.add((source, link_text))

                    if corpus_links != cached_links:
                        actions.append(
                            {
                                "target": "graph_cache",
                                "action": "full_rebuild",
                                "reason": "edge_set_mismatch",
                            }
                        )

    if not actions:
        # Cache is consistent — scan for stale wiki-links (report-only, do NOT modify note content)
        broken = graph.get_broken_links()
        stale_actions: list[dict[str, Any]] = []
        for source_id, source_title, link_target in broken:
            stale_actions.append(
                {
                    "target": "wiki_link",
                    "action": "stale_link",
                    "source_id": source_id,
                    "source_title": source_title,
                    "link_target": link_target,
                    "reason": "target_slug_not_found",
                }
            )

        if not stale_actions:
            return _make_result("graph", dry_run, status="noop", scanned=len(corpus_docs))

        # Stale links detected — report them (no writes in either dry_run or real run)
        return _make_result(
            "graph",
            dry_run,
            status="ok",
            scanned=len(corpus_docs),
            actions=stale_actions,
        )

    if dry_run:
        return _make_result(
            "graph",
            dry_run,
            status="ok",
            scanned=len(corpus_docs),
            repaired=len(actions),
            actions=actions,
        )

    # Apply repairs: full rebuild (always prefer full rebuild for graph)
    repaired = 0
    with tracer.start_as_current_span("lithos.reconcile.apply") as apply_span:
        apply_span.set_attribute("lithos.reconcile.scope", "graph")
        apply_span.set_attribute("lithos.reconcile.backend", "graph")
        try:
            fresh_graph = KnowledgeGraph(config)
            fresh_graph.clear()
            for doc in corpus_docs:
                fresh_graph.add_document(doc)
            fresh_graph.save_cache()
            repaired = 1
        except Exception as exc:
            logger.error("Failed to rebuild graph cache: %s", exc)
            failures.append({"code": "graph_rebuild_failed", "detail": str(exc)})

    n_failed = len(failures)
    status: ReconcileStatus = "failed" if n_failed > 0 else "ok"

    lithos_metrics.reconcile_ops.add(1, {"scope": "graph", "status": status})
    return _make_result(
        "graph",
        dry_run,
        status=status,
        scanned=len(corpus_docs),
        repaired=repaired,
        failed=n_failed,
        actions=actions,
        failures=failures,
    )


def _reconcile_provenance_projection(config: LithosConfig, dry_run: bool) -> dict[str, Any]:
    """Reconcile projected LCMA provenance edges.

    Returns supported=False before edges.db / LCMA projection storage exists.
    Returns supported=True with status=noop once that storage is present
    (full repair logic is deferred to after LCMA rollout).
    """
    edges_db = config.storage.data_dir / ".lithos" / "edges.db"
    if not edges_db.exists():
        return _make_result(
            "provenance_projection",
            dry_run,
            supported=False,
            status="noop",
            actions=[{"reason": "not_enabled"}],
        )

    # Projection store is present but full LCMA repair is not yet implemented.
    lithos_metrics.reconcile_ops.add(1, {"scope": "provenance_projection", "status": "noop"})
    return _make_result(
        "provenance_projection",
        dry_run,
        supported=True,
        status="noop",
        actions=[{"reason": "not_implemented"}],
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def reconcile(
    scope: ReconcileScope = "all",
    dry_run: bool = False,
    config: LithosConfig | None = None,
) -> dict[str, Any]:
    """Reconcile derived projections against the markdown source of truth.

    Core internal logic — not exposed as an MCP tool.

    Args:
        scope: Which projections to reconcile (indices / graph /
               provenance_projection / all).
        dry_run: If True, compute diffs but make no writes.  The returned
                 ``actions`` list describes what a real run would do.
        config: Configuration.  Uses the global config if not provided.

    Returns:
        Structured result dict with keys: scope, dry_run, supported, status,
        summary (scanned/repaired/failed/skipped), actions, failures.
    """
    cfg = config or get_config()
    tracer = get_tracer()

    with tracer.start_as_current_span("lithos.reconcile") as span:
        span.set_attribute("lithos.reconcile.scope", scope)
        span.set_attribute("lithos.reconcile.dry_run", dry_run)

        if scope == "indices":
            result = await _reconcile_indices(cfg, dry_run)

        elif scope == "graph":
            result = await _reconcile_graph(cfg, dry_run)

        elif scope == "provenance_projection":
            result = _reconcile_provenance_projection(cfg, dry_run)

        elif scope == "all":
            # Run in prescribed order; errors in one scope must not prevent others.
            sub_results: list[dict[str, Any]] = []

            try:
                sub_results.append(await _reconcile_indices(cfg, dry_run))
            except Exception as exc:
                logger.error("Unhandled error in indices reconcile: %s", exc)
                sub_results.append(
                    _make_result(
                        "indices",
                        dry_run,
                        status="failed",
                        failed=1,
                        failures=[{"code": "internal_error", "detail": str(exc)}],
                    )
                )

            try:
                sub_results.append(await _reconcile_graph(cfg, dry_run))
            except Exception as exc:
                logger.error("Unhandled error in graph reconcile: %s", exc)
                sub_results.append(
                    _make_result(
                        "graph",
                        dry_run,
                        status="failed",
                        failed=1,
                        failures=[{"code": "internal_error", "detail": str(exc)}],
                    )
                )

            try:
                sub_results.append(_reconcile_provenance_projection(cfg, dry_run))
            except Exception as exc:
                logger.error("Unhandled error in provenance_projection reconcile: %s", exc)
                sub_results.append(
                    _make_result(
                        "provenance_projection",
                        dry_run,
                        status="failed",
                        failed=1,
                        failures=[{"code": "internal_error", "detail": str(exc)}],
                    )
                )

            all_actions: list[dict[str, Any]] = []
            all_failures: list[dict[str, Any]] = []
            total_scanned = 0
            total_repaired = 0
            total_failed = 0
            total_skipped = 0
            statuses: list[ReconcileStatus] = []

            for r in sub_results:
                all_actions.extend(r["actions"])
                all_failures.extend(r["failures"])
                total_scanned = max(total_scanned, r["summary"]["scanned"])
                total_repaired += r["summary"]["repaired"]
                total_failed += r["summary"]["failed"]
                total_skipped += r["summary"]["skipped"]
                statuses.append(r["status"])

            agg_status = _aggregate_status(statuses)
            lithos_metrics.reconcile_ops.add(1, {"scope": "all", "status": agg_status})

            result = _make_result(
                "all",
                dry_run,
                supported=True,
                status=agg_status,
                scanned=total_scanned,
                repaired=total_repaired,
                failed=total_failed,
                skipped=total_skipped,
                actions=all_actions,
                failures=all_failures,
            )

        else:
            result = _make_result(
                scope,
                dry_run,
                status="failed",
                failures=[{"code": "internal_error", "detail": f"unknown scope: {scope!r}"}],
            )

        span.set_attribute("lithos.reconcile.status", result["status"])
        return result
