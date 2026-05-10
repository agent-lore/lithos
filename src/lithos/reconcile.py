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
from lithos.graph import GraphReconcileAction, KnowledgeGraph
from lithos.knowledge import KnowledgeManager
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
    """Reconcile the search indices via :class:`KnowledgeManager` (#226).

    KM owns the corpus scan and orchestrates the search slice; this function
    is a thin adapter that translates the structured
    :class:`~lithos.knowledge.ReconcilePlan` /
    :class:`~lithos.knowledge.ReconcileResult` back into the legacy dict shape
    the public ``reconcile()`` aggregator returns.
    """
    tracer = get_tracer()

    logger.info(
        "reconcile indices started: dry_run=%s",
        dry_run,
        extra={"scope": "indices", "dry_run": dry_run},
    )

    knowledge = KnowledgeManager(config=config)
    try:
        with tracer.start_as_current_span("lithos.reconcile.scan") as scan_span:
            scan_span.set_attribute("lithos.reconcile.scope", "indices")
            search = await SearchEngine.create(config)
            with tracer.start_as_current_span("lithos.reconcile.diff") as diff_span:
                diff_span.set_attribute("lithos.reconcile.scope", "indices")
                plan = await knowledge.plan_reconcile(search)
    except Exception as exc:
        logger.error("Failed to plan indices reconcile: %s", exc)
        return _make_result(
            "indices",
            dry_run,
            status="failed",
            failed=1,
            failures=[{"code": "internal_error", "detail": str(exc)}],
        )

    assert plan.search is not None  # search engine was passed → slice is populated
    search_plan = plan.search
    actions = [
        {"backend": a.backend, "action": a.action, "reason": a.reason} for a in search_plan.actions
    ]
    scanned = search_plan.scanned

    if search_plan.is_noop:
        return _make_result("indices", dry_run, status="noop", scanned=scanned)

    if dry_run:
        return _make_result(
            "indices",
            dry_run,
            status="ok",
            scanned=scanned,
            repaired=len(actions),
            actions=actions,
        )

    with tracer.start_as_current_span("lithos.reconcile.apply") as apply_span:
        apply_span.set_attribute("lithos.reconcile.scope", "indices")
        result = await knowledge.apply_reconcile(plan, search)

    assert result.search is not None
    search_result = result.search
    repaired = search_result.repaired
    failures = [
        {"code": "index_rebuild_failed", "backend": f.backend, "detail": f.detail}
        for f in search_result.failed
    ]
    n_failed = len(failures)

    if n_failed == 0:
        status: ReconcileStatus = "ok"
    elif repaired == 0:
        status = "failed"
    else:
        status = "partial_failure"

    lithos_metrics.reconcile_ops.add(1, {"scope": "indices", "status": status})
    logger.info(
        "reconcile indices complete: status=%s scanned=%d repaired=%d failed=%d dry_run=%s",
        status,
        scanned,
        repaired,
        n_failed,
        dry_run,
        extra={
            "scope": "indices",
            "status": status,
            "scanned": scanned,
            "repaired": repaired,
            "failed": n_failed,
            "dry_run": dry_run,
        },
    )
    return _make_result(
        "indices",
        dry_run,
        status=status,
        scanned=scanned,
        repaired=repaired,
        failed=n_failed,
        actions=actions,
        failures=failures,
    )


def _action_to_dict(action: GraphReconcileAction) -> dict[str, Any]:
    """Translate a structured graph action into the legacy dict shape."""
    payload: dict[str, Any] = {
        "target": action.target,
        "action": action.action,
        "reason": action.reason,
    }
    if action.source_id is not None:
        payload["source_id"] = action.source_id
    if action.source_title is not None:
        payload["source_title"] = action.source_title
    if action.link_target is not None:
        payload["link_target"] = action.link_target
    if action.corpus_count is not None:
        payload["corpus_count"] = action.corpus_count
    if action.cached_count is not None:
        payload["cached_count"] = action.cached_count
    return payload


async def _reconcile_graph(config: LithosConfig, dry_run: bool) -> dict[str, Any]:
    """Reconcile the wiki-link graph cache via :class:`KnowledgeManager`.

    KM owns the corpus scan and dispatches the graph slice; this function is
    a thin adapter that translates the structured
    :class:`~lithos.knowledge.ReconcilePlan` /
    :class:`~lithos.knowledge.ReconcileResult` back into the legacy dict shape
    the public :func:`reconcile` aggregator returns.

    When the cache is already consistent (no node/edge drift), the planner
    surfaces stale wiki-links as report-only ``stale_link`` actions. Stale-link
    detection is skipped when the cache itself needs a rebuild; the caller
    must reconcile again after repair to surface stale links.
    """
    tracer = get_tracer()

    logger.info(
        "reconcile graph started: dry_run=%s",
        dry_run,
        extra={"scope": "graph", "dry_run": dry_run},
    )

    knowledge = KnowledgeManager(config=config)
    graph = KnowledgeGraph(config)
    try:
        with tracer.start_as_current_span("lithos.reconcile.scan") as scan_span:
            scan_span.set_attribute("lithos.reconcile.scope", "graph")
            with tracer.start_as_current_span("lithos.reconcile.diff") as diff_span:
                diff_span.set_attribute("lithos.reconcile.scope", "graph")
                plan = await knowledge.plan_reconcile(graph=graph)
    except Exception as exc:
        logger.error("Failed to plan graph reconcile: %s", exc)
        return _make_result(
            "graph",
            dry_run,
            status="failed",
            failed=1,
            failures=[{"code": "internal_error", "detail": str(exc)}],
        )

    assert plan.graph is not None  # graph engine was passed → slice is populated
    graph_plan = plan.graph
    actions = [_action_to_dict(a) for a in graph_plan.actions]
    scanned = graph_plan.scanned

    if graph_plan.is_noop:
        return _make_result("graph", dry_run, status="noop", scanned=scanned)

    if not graph_plan.needs_rebuild:
        # Stale links only — report them (no writes in either dry_run or real run).
        return _make_result(
            "graph",
            dry_run,
            status="ok",
            scanned=scanned,
            actions=actions,
        )

    if dry_run:
        return _make_result(
            "graph",
            dry_run,
            status="ok",
            scanned=scanned,
            repaired=len(actions),
            actions=actions,
        )

    with tracer.start_as_current_span("lithos.reconcile.apply") as apply_span:
        apply_span.set_attribute("lithos.reconcile.scope", "graph")
        apply_span.set_attribute("lithos.reconcile.backend", "graph")
        result = await knowledge.apply_reconcile(plan, graph=graph)

    assert result.graph is not None
    graph_result = result.graph
    repaired = graph_result.repaired
    failures = [{"code": "graph_rebuild_failed", "detail": f.detail} for f in graph_result.failed]
    n_failed = len(failures)
    status: ReconcileStatus = "failed" if n_failed > 0 else "ok"

    lithos_metrics.reconcile_ops.add(1, {"scope": "graph", "status": status})
    logger.info(
        "reconcile graph complete: status=%s scanned=%d repaired=%d failed=%d dry_run=%s",
        status,
        scanned,
        repaired,
        n_failed,
        dry_run,
        extra={
            "scope": "graph",
            "status": status,
            "scanned": scanned,
            "repaired": repaired,
            "failed": n_failed,
            "dry_run": dry_run,
        },
    )
    return _make_result(
        "graph",
        dry_run,
        status=status,
        scanned=scanned,
        repaired=repaired,
        failed=n_failed,
        actions=actions,
        failures=failures,
    )


async def _reconcile_provenance_projection(config: LithosConfig, dry_run: bool) -> dict[str, Any]:
    """Reconcile projected LCMA ``derived_from`` edges in ``edges.db``.

    Walks every note's frontmatter ``derived_from_ids`` and ensures
    ``edges.db`` has a matching ``type='derived_from'`` edge, creating
    missing edges and removing orphan projections. Returns
    ``supported=False`` when ``edges.db`` does not exist (LCMA disabled).

    Markdown/frontmatter is the source of truth — this function only
    mutates the derived edge projection.
    """
    from lithos.provenance import EdgeStore, _project_provenance_to_edges

    edges_db = config.storage.data_dir / ".lithos" / "edges.db"
    if not edges_db.exists():
        return _make_result(
            "provenance_projection",
            dry_run,
            supported=False,
            status="noop",
            actions=[{"reason": "not_enabled"}],
        )

    edge_store = EdgeStore(config=config)
    knowledge = KnowledgeManager(config=config)

    try:
        counts = await _project_provenance_to_edges(edge_store, knowledge, dry_run=dry_run)
    except Exception as exc:
        logger.error("provenance_projection reconcile failed: %s", exc)
        lithos_metrics.reconcile_ops.add(1, {"scope": "provenance_projection", "status": "failed"})
        return _make_result(
            "provenance_projection",
            dry_run,
            supported=True,
            status="failed",
            failed=1,
            failures=[{"code": "internal_error", "detail": str(exc)}],
        )
    finally:
        # Close the persistent connection so the aiosqlite worker thread
        # exits cleanly. Otherwise it survives function return, eventually
        # touches a torn-down event loop, and emits "Event loop is closed"
        # warnings (and on CI, hangs the whole job until timeout) (#172).
        await edge_store.close()

    created = int(counts.get("created", 0))
    removed = int(counts.get("removed", 0))
    repaired = created + removed
    status: ReconcileStatus = "ok" if repaired > 0 else "noop"
    lithos_metrics.reconcile_ops.add(1, {"scope": "provenance_projection", "status": status})
    return _make_result(
        "provenance_projection",
        dry_run,
        supported=True,
        status=status,
        repaired=repaired,
        actions=[{"created": created, "removed": removed}],
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

    logger.info(
        "reconcile: scope=%s dry_run=%s",
        scope,
        dry_run,
        extra={"scope": scope, "dry_run": dry_run},
    )
    with tracer.start_as_current_span("lithos.reconcile") as span:
        span.set_attribute("lithos.reconcile.scope", scope)
        span.set_attribute("lithos.reconcile.dry_run", dry_run)

        if scope == "indices":
            result = await _reconcile_indices(cfg, dry_run)

        elif scope == "graph":
            result = await _reconcile_graph(cfg, dry_run)

        elif scope == "provenance_projection":
            result = await _reconcile_provenance_projection(cfg, dry_run)

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
                sub_results.append(await _reconcile_provenance_projection(cfg, dry_run))
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
        logger.info(
            "reconcile complete: scope=%s status=%s scanned=%d repaired=%d failed=%d dry_run=%s",
            result["scope"],
            result["status"],
            result["summary"]["scanned"],
            result["summary"]["repaired"],
            result["summary"]["failed"],
            dry_run,
            extra={
                "scope": result["scope"],
                "status": result["status"],
                "scanned": result["summary"]["scanned"],
                "repaired": result["summary"]["repaired"],
                "failed": result["summary"]["failed"],
                "dry_run": dry_run,
            },
        )
        return result
