"""The `lithos reconcile` operator surface: scope policy and result shaping.

ADR-0001 folded reconciliation itself onto :class:`KnowledgeManager` — it owns
the corpus, so it owns the scan and dispatches a private plan/apply pair to each
derived view. That migration is complete, and with it the Core-tier
``lithos.reconcile`` peer module died.

What lived on is not reconciliation: it is the *operator surface* over it —
which scopes exist, how their statuses aggregate, and the result dict
``lithos reconcile --json-output`` promises. That is entrypoint concern, so it
lives here, beside the CLI, rather than in Core.

Markdown/frontmatter is the source of truth and is NEVER mutated here. Only
derived state (indices, graph cache, provenance edges) may be repaired.

Scope behaviour:
    indices              — repairs Tantivy and ChromaDB from the markdown corpus
    graph                — repairs the wiki-link graph cache
    provenance_projection— repairs projected LCMA edges (returns supported=False
                           before edges.db / LCMA storage exists)
    all                  — runs the three scopes in that order and aggregates

Why this does not use ``build_pipeline``: reconcile must not initialise storage
just to inspect it. ``build_pipeline`` opens the EdgeStore eagerly, which creates
``edges.db``; routing through it would make even ``--dry-run`` leave a file
behind, and would turn "LCMA was never enabled" into "LCMA is enabled and empty".
So this module builds exactly the engines each scope needs — and nothing else.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from lithos.config import LithosConfig, get_config
from lithos.graph import GraphReconcileAction, KnowledgeGraph
from lithos.knowledge import KnowledgeManager
from lithos.search import SearchEngine
from lithos.telemetry import get_tracer, lithos_metrics

if TYPE_CHECKING:
    from lithos.knowledge import ReconcilePlan
    from lithos.provenance import ProvenanceProjection

logger = logging.getLogger(__name__)

ReconcileScope = Literal["all", "indices", "graph", "provenance_projection"]
ReconcileStatus = Literal["ok", "noop", "partial_failure", "failed"]

# The slices each scope requests, in the order `all` reports them.
_SCOPE_SLICES: dict[str, tuple[str, ...]] = {
    "indices": ("indices",),
    "graph": ("graph",),
    "provenance_projection": ("provenance_projection",),
    "all": ("indices", "graph", "provenance_projection"),
}


# ---------------------------------------------------------------------------
# Result shaping
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
    """Build a structured reconcile result dict.

    This shape is the CLI's ``--json-output`` contract; keep it stable.
    """
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


def _internal_error(scope: str, dry_run: bool, exc: Exception) -> dict[str, Any]:
    return _make_result(
        scope,
        dry_run,
        status="failed",
        failed=1,
        failures=[{"code": "internal_error", "detail": str(exc)}],
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


def _edge_action_dict(created: int, removed: int, resynced: int) -> dict[str, int]:
    """Legacy dict shape; ``resynced`` is only present when non-zero so existing
    CLI consumers and tests asserting ``{created, removed}`` equality keep
    working when no column drift was repaired."""
    payload = {"created": created, "removed": removed}
    if resynced:
        payload["resynced"] = resynced
    return payload


# ---------------------------------------------------------------------------
# Per-slice shaping — each takes the shared plan, applies its own slice
# ---------------------------------------------------------------------------


async def _finish_indices(
    knowledge: KnowledgeManager,
    plan: ReconcilePlan,
    search: SearchEngine,
    dry_run: bool,
) -> dict[str, Any]:
    tracer = get_tracer()
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
        result = await knowledge.apply_reconcile(plan, search=search)

    assert result.search is not None
    search_result = result.search
    repaired = search_result.repaired
    failures = [
        {"code": "index_rebuild_failed", "backend": f.backend, "detail": f.detail}
        for f in search_result.failed
    ]
    n_failed = len(failures)

    # Three-way: a partial backend failure that still repaired something is not
    # the same as repairing nothing.
    if n_failed == 0:
        status: ReconcileStatus = "ok"
    elif repaired == 0:
        status = "failed"
    else:
        status = "partial_failure"

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


async def _finish_graph(
    knowledge: KnowledgeManager,
    plan: ReconcilePlan,
    graph: KnowledgeGraph,
    dry_run: bool,
) -> dict[str, Any]:
    tracer = get_tracer()
    assert plan.graph is not None  # graph engine was passed → slice is populated
    graph_plan = plan.graph
    actions = [_action_to_dict(a) for a in graph_plan.actions]
    scanned = graph_plan.scanned

    if graph_plan.is_noop:
        return _make_result("graph", dry_run, status="noop", scanned=scanned)

    if not graph_plan.needs_rebuild:
        # Stale links only — report them (no writes in either dry_run or real
        # run). Stale-link detection is skipped when the cache itself needs a
        # rebuild; the caller must reconcile again after repair to surface them.
        return _make_result("graph", dry_run, status="ok", scanned=scanned, actions=actions)

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


async def _finish_provenance(
    knowledge: KnowledgeManager,
    plan: ReconcilePlan,
    projection: ProvenanceProjection,
    dry_run: bool,
) -> dict[str, Any]:
    tracer = get_tracer()
    assert plan.provenance is not None  # projection was passed → slice present
    prov_plan = plan.provenance

    if not prov_plan.supported:
        return _make_result(
            "provenance_projection",
            dry_run,
            supported=False,
            status="noop",
            actions=[{"reason": "not_enabled"}],
        )

    created_planned = sum(1 for a in prov_plan.actions if a.action == "create")
    removed_planned = sum(1 for a in prov_plan.actions if a.action == "remove")
    resynced_planned = sum(1 for a in prov_plan.actions if a.action == "resync")

    if prov_plan.is_noop:
        return _make_result(
            "provenance_projection",
            dry_run,
            status="noop",
            repaired=0,
            actions=[_edge_action_dict(0, 0, 0)],
        )

    if dry_run:
        return _make_result(
            "provenance_projection",
            dry_run,
            status="ok",
            repaired=created_planned + removed_planned + resynced_planned,
            actions=[_edge_action_dict(created_planned, removed_planned, resynced_planned)],
        )

    with tracer.start_as_current_span("lithos.reconcile.apply") as apply_span:
        apply_span.set_attribute("lithos.reconcile.scope", "provenance_projection")
        apply_span.set_attribute("lithos.reconcile.backend", "provenance_projection")
        result = await knowledge.apply_reconcile(plan, projection=projection)

    assert result.provenance is not None
    prov_result = result.provenance
    created, removed, resynced = prov_result.created, prov_result.removed, prov_result.resynced
    failures = [{"code": "projection_apply_failed", "detail": f.detail} for f in prov_result.failed]
    n_failed = len(failures)
    status: ReconcileStatus = "failed" if n_failed > 0 else "ok"

    return _make_result(
        "provenance_projection",
        dry_run,
        status=status,
        repaired=created + removed + resynced,
        failed=n_failed,
        actions=[_edge_action_dict(created, removed, resynced)],
        failures=failures,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def _run_slices(
    cfg: LithosConfig,
    dry_run: bool,
    requested: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Plan once, then apply each requested slice in isolation.

    One :meth:`KnowledgeManager.plan_reconcile` covers every requested slice —
    the seam ADR-0001 established, and the one ``server._rebuild_indices`` and
    ``cli reindex`` already use. It scans the corpus once; the three per-scope
    functions this replaced scanned it three times over three managers.

    Applies stay isolated per slice so one failing view cannot block repair of
    the others — that is what ``partial_failure`` means. A *plan* failure is not
    isolated, and should not be: it is almost always ``CorpusScanError``, which
    is a statement about the corpus, not about any one view (see task 681ac952
    PR1c — reconciling from a partial corpus deletes live data).
    """
    from lithos.edge_store import EdgeStore
    from lithos.provenance import ProvenanceProjection

    tracer = get_tracer()
    knowledge = KnowledgeManager(config=cfg)

    search = await SearchEngine.create(cfg) if "indices" in requested else None
    graph = KnowledgeGraph(cfg) if "graph" in requested else None

    projection: ProvenanceProjection | None = None
    edge_store: EdgeStore | None = None
    if "provenance_projection" in requested and cfg.storage.edges_db_path.exists():
        # Only opened once edges.db is known to exist — see the module docstring
        # on why reconcile must not initialise storage. One store, injected, so
        # the projection cannot self-create a second (ADR-0006 Slice 1, #263).
        edge_store = EdgeStore(cfg)
        await edge_store.open()
        projection = await ProvenanceProjection.create(cfg, edge_store=edge_store)

    try:
        try:
            with tracer.start_as_current_span("lithos.reconcile.plan") as plan_span:
                plan_span.set_attribute("lithos.reconcile.scopes", ",".join(requested))
                plan = await knowledge.plan_reconcile(
                    search=search, graph=graph, projection=projection
                )
        except Exception as exc:
            logger.error(
                "reconcile plan failed: %s",
                exc,
                extra={"scopes": list(requested), "dry_run": dry_run},
            )
            return [_internal_error(name, dry_run, exc) for name in requested]

        results: list[dict[str, Any]] = []
        for name in requested:
            if name == "provenance_projection" and projection is None:
                # edges.db absent — LCMA storage was never initialised.
                results.append(
                    _make_result(
                        name,
                        dry_run,
                        supported=False,
                        status="noop",
                        actions=[{"reason": "not_enabled"}],
                    )
                )
                continue
            try:
                if name == "indices":
                    assert search is not None
                    results.append(await _finish_indices(knowledge, plan, search, dry_run))
                elif name == "graph":
                    assert graph is not None
                    results.append(await _finish_graph(knowledge, plan, graph, dry_run))
                else:
                    assert projection is not None
                    results.append(await _finish_provenance(knowledge, plan, projection, dry_run))
            except Exception as exc:
                logger.error(
                    "reconcile %s failed: %s", name, exc, extra={"scope": name, "dry_run": dry_run}
                )
                results.append(_internal_error(name, dry_run, exc))
        return results
    finally:
        if edge_store is not None:
            # Close the persistent connection so the aiosqlite worker thread
            # exits cleanly. Otherwise it survives function return, eventually
            # touches a torn-down event loop, and emits "Event loop is closed"
            # warnings (and on CI, hangs the whole job until timeout) (#172).
            await edge_store.close()


async def reconcile(
    scope: ReconcileScope = "all",
    dry_run: bool = False,
    config: LithosConfig | None = None,
) -> dict[str, Any]:
    """Reconcile derived projections against the markdown source of truth.

    The operator surface behind ``lithos reconcile``; not an MCP tool.

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

        requested = _SCOPE_SLICES.get(scope)
        if requested is None:
            # Reachable: the CLI passes a str straight through.
            result = _make_result(
                scope,
                dry_run,
                status="failed",
                failures=[{"code": "internal_error", "detail": f"unknown scope: {scope!r}"}],
            )
        else:
            sub_results = await _run_slices(cfg, dry_run, requested)

            if scope == "all":
                statuses: list[ReconcileStatus] = [r["status"] for r in sub_results]
                # scanned folds with max, not sum: every scope scans the same
                # corpus, so summing would report it several times over.
                result = _make_result(
                    "all",
                    dry_run,
                    status=_aggregate_status(statuses),
                    scanned=max((r["summary"]["scanned"] for r in sub_results), default=0),
                    repaired=sum(r["summary"]["repaired"] for r in sub_results),
                    failed=sum(r["summary"]["failed"] for r in sub_results),
                    skipped=sum(r["summary"]["skipped"] for r in sub_results),
                    actions=[a for r in sub_results for a in r["actions"]],
                    failures=[f for r in sub_results for f in r["failures"]],
                )
            else:
                result = sub_results[0]

            # One counter per scope per run, on every path. The module this
            # replaced emitted for indices/graph only after a successful apply
            # while provenance emitted on every path — an asymmetry no one chose.
            # It was invisible until task ba8d7f25 PR2a made CLI telemetry live
            # at all, so nothing depends on the gaps.
            for r in sub_results:
                lithos_metrics.reconcile_ops.add(1, {"scope": r["scope"], "status": r["status"]})
            if scope == "all":
                lithos_metrics.reconcile_ops.add(1, {"scope": "all", "status": result["status"]})

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
