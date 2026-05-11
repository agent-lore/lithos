"""ProvenanceProjection — corpus-derived edges as a Module facade.

See docs/adr/0004-provenance-projection-module.md.

This Module wraps the SQLite-backed edge store and exposes a public
**read-only** surface to callers plus a package-private plan/apply pair
called only from :class:`~lithos.knowledge.KnowledgeManager`. The store
and the projection helpers under ``lcma/edges.py`` are package-internal
implementation details and must not be imported from outside this module.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from lithos.config import LithosConfig
from lithos.knowledge import KnowledgeDocument, derive_namespace

# This module is the single legitimate import site for the package-internal
# edge store and the corpus-to-edges projection helpers (ADR-0004,
# issue #251). The class and helpers stay importable from here as
# **undocumented transitional internals** — they are deliberately NOT
# listed in ``__all__``. The enrich worker's full sweep
# (``lcma/enrich.py``) still consumes ``_project_provenance_to_edges``;
# migrating that path through the projection's plan/apply is a separate
# follow-up. The import-graph guard in #262 will enforce the rule
# mechanically.
from lithos.lcma.edges import (
    EdgeStore,
    _project_node_provenance,  # noqa: F401  re-exported for transitional callers
    _project_provenance_to_edges,  # noqa: F401  consumed by lcma/enrich.py full_sweep
)

# Public module API — only the façade and its plan/apply value types.
__all__ = [
    "ProvenancePlan",
    "ProvenanceProjection",
    "ProvenanceReconcileAction",
    "ProvenanceReconcileFailure",
    "ProvenanceResult",
]


@dataclass(frozen=True)
class ProvenanceReconcileAction:
    """A single create or remove planned against the projection.

    ``target='projection_edge'`` is the only target today. ``edge_id`` is
    populated for ``remove`` actions (so apply can target the row by id)
    and is ``None`` for ``create`` actions (the row does not exist yet).
    """

    target: Literal["projection_edge"]
    action: Literal["create", "remove"]
    from_id: str
    to_id: str
    namespace: str
    edge_id: str | None = None


@dataclass(frozen=True)
class ProvenanceReconcileFailure:
    """An action that errored while applying a plan."""

    detail: str


@dataclass(frozen=True)
class ProvenancePlan:
    """The dry-run output of :meth:`ProvenanceProjection._plan_reconcile_to`.

    ``supported`` is ``False`` when ``edges.db`` does not exist on disk
    (LCMA storage not initialised). In that case ``actions`` is empty and
    the plan is a no-op.
    """

    actions: tuple[ProvenanceReconcileAction, ...]
    scanned: int
    supported: bool = True

    @property
    def is_noop(self) -> bool:
        """True when no drift was detected."""
        return not self.actions


@dataclass(frozen=True)
class ProvenanceResult:
    """The outcome of applying a :class:`ProvenancePlan`."""

    actions: tuple[ProvenanceReconcileAction, ...]
    created: int
    removed: int
    failed: tuple[ProvenanceReconcileFailure, ...]
    scanned: int
    supported: bool = True


class ProvenanceProjection:
    """Module facade over the corpus-derived edge projection.

    Construction follows ADR-0002's eager-init pattern: callers always
    obtain the projection via :meth:`create`, which opens the underlying
    store before returning, so no caller observes a half-initialised
    projection.
    """

    # The Module-owned predicate that scopes plan/apply to corpus-derived
    # edges. Today: ``provenance_type='frontmatter'``. Grows internally
    # as the projection mirrors more frontmatter-declared lineage (ADR-0004).
    # NEVER exposed to callers — they see the value type, not the predicate.
    _CORPUS_DERIVED_PROVENANCE_TYPE: str = "frontmatter"
    _CORPUS_DERIVED_EDGE_TYPE: str = "derived_from"

    def __init__(self, config: LithosConfig | None = None) -> None:
        self._config = config
        self._edge_store: EdgeStore = EdgeStore(config)

    @classmethod
    async def create(cls, config: LithosConfig | None = None) -> ProvenanceProjection:
        """Construct the projection and open its underlying store eagerly."""
        projection = cls(config)
        await projection._edge_store.open()
        return projection

    async def close(self) -> None:
        """Close the underlying store. Idempotent."""
        await self._edge_store.close()

    # ---- package-private plan/apply (ADR-0001 step 3 / ADR-0004) ----

    async def _plan_reconcile_to(self, docs: Iterable[KnowledgeDocument]) -> ProvenancePlan:
        """Compute a :class:`ProvenancePlan` describing drift against *docs*.

        Package-private — agents call ``KnowledgeManager.plan_reconcile``,
        which delegates here. Pure: never mutates the store.

        Scoping is owned by this Module: only edges matching the
        ``provenance_type='frontmatter'`` predicate are candidates for
        removal. Agent-asserted edges with any other ``provenance_type``
        survive reconcile untouched.
        """
        snapshot = tuple(docs)

        if not self._edge_store.db_path.exists():
            return ProvenancePlan(actions=(), scanned=len(snapshot), supported=False)

        desired: set[tuple[str, str, str]] = set()
        for doc in snapshot:
            if not doc.metadata.derived_from_ids:
                continue
            ns = doc.metadata.namespace or derive_namespace(doc.path)
            for source_id in doc.metadata.derived_from_ids:
                desired.add((doc.id, source_id, ns))

        raw = await self._edge_store.list_edges(edge_type=self._CORPUS_DERIVED_EDGE_TYPE)
        existing_map: dict[tuple[str, str, str], str] = {}
        for e in raw:
            if e["provenance_type"] != self._CORPUS_DERIVED_PROVENANCE_TYPE:
                continue
            key = (str(e["from_id"]), str(e["to_id"]), str(e["namespace"]))
            existing_map[key] = str(e["edge_id"])

        existing_keys = set(existing_map.keys())
        to_create = desired - existing_keys
        to_remove = existing_keys - desired

        actions: list[ProvenanceReconcileAction] = []
        for from_id, to_id, ns in sorted(to_create):
            actions.append(
                ProvenanceReconcileAction(
                    target="projection_edge",
                    action="create",
                    from_id=from_id,
                    to_id=to_id,
                    namespace=ns,
                )
            )
        for key in sorted(to_remove):
            from_id, to_id, ns = key
            actions.append(
                ProvenanceReconcileAction(
                    target="projection_edge",
                    action="remove",
                    from_id=from_id,
                    to_id=to_id,
                    namespace=ns,
                    edge_id=existing_map[key],
                )
            )

        return ProvenancePlan(actions=tuple(actions), scanned=len(snapshot), supported=True)

    async def _apply_reconcile(self, plan: ProvenancePlan) -> ProvenanceResult:
        """Execute *plan* against the projection store. Idempotent."""
        if not plan.supported:
            return ProvenanceResult(
                actions=(),
                created=0,
                removed=0,
                failed=(),
                scanned=plan.scanned,
                supported=False,
            )

        created = 0
        removed = 0
        failures: list[ProvenanceReconcileFailure] = []
        remove_edge_ids: list[str] = []

        for act in plan.actions:
            if act.action == "create":
                try:
                    await self._edge_store.upsert(
                        from_id=act.from_id,
                        to_id=act.to_id,
                        edge_type=self._CORPUS_DERIVED_EDGE_TYPE,
                        weight=1.0,
                        namespace=act.namespace,
                        provenance_type=self._CORPUS_DERIVED_PROVENANCE_TYPE,
                    )
                    created += 1
                except Exception as exc:
                    failures.append(ProvenanceReconcileFailure(detail=str(exc)))
            elif act.action == "remove":
                assert act.edge_id is not None
                remove_edge_ids.append(act.edge_id)

        if remove_edge_ids:
            try:
                await self._edge_store.delete_edges(edge_ids=remove_edge_ids)
                removed = len(remove_edge_ids)
            except Exception as exc:
                failures.append(ProvenanceReconcileFailure(detail=str(exc)))

        return ProvenanceResult(
            actions=plan.actions,
            created=created,
            removed=removed,
            failed=tuple(failures),
            scanned=plan.scanned,
            supported=True,
        )

    # ---- public read API ----

    async def list_edges(
        self,
        *,
        from_id: str | None = None,
        to_id: str | None = None,
        edge_type: str | None = None,
        namespace: str | None = None,
    ) -> list[dict[str, object]]:
        return await self._edge_store.list_edges(
            from_id=from_id,
            to_id=to_id,
            edge_type=edge_type,
            namespace=namespace,
        )

    async def get_edge(self, edge_id: str) -> dict[str, object] | None:
        return await self._edge_store.get_edge(edge_id)

    async def count(self, *, namespace: str | None = None) -> int:
        return await self._edge_store.count(namespace=namespace)

    async def list_edges_between(
        self,
        node_ids: list[str],
        *,
        edge_type: str | None = None,
        namespace: str | None = None,
    ) -> list[dict[str, object]]:
        return await self._edge_store.list_edges_between(
            node_ids,
            edge_type=edge_type,
            namespace=namespace,
        )
