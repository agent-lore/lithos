"""ProvenanceProjection — corpus-derived edges as a Module facade.

See docs/adr/0004-provenance-projection-module.md.

This Module wraps the SQLite-backed edge store. It exposes a public read
surface, the public ``reconcile_corpus`` / ``reconcile_node`` entry points
(used by the enrich worker to keep ``derived_from`` edges fresh between
Reconciles), and a package-private plan/apply pair driven by
:class:`~lithos.knowledge.KnowledgeManager` during whole-pipeline Reconcile.
The underlying edge store is a package-internal detail reached only through
this Module.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from lithos.config import LithosConfig

# ``EdgeStore`` lives at the public peer module ``lithos.edge_store`` after
# ADR-0006 Slice 1 (issue #263) — ``ProvenanceProjection`` owns the
# projection-class rows, ``CorpusIntake.assert_edge`` owns the asserted-class
# rows, and both share an injected store. The corpus-to-edges projection is now
# owned entirely here (reconcile_corpus / reconcile_node); the former
# ``lcma/edges.py`` helpers are gone (task 681ac952 PR1c).
from lithos.edge_store import EdgeStore
from lithos.frontmatter_codec import KnowledgeDocument, derive_namespace

logger = logging.getLogger(__name__)

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
    """A single create, remove, or resync planned against the projection.

    ``target='projection_edge'`` is the only target today.

    - ``action='create'``: the (from_id, to_id, namespace) key is desired by
      frontmatter but absent from the projection. ``edge_id`` is ``None``.
    - ``action='remove'``: the key exists in the projection with the
      frontmatter predicate but is no longer desired. ``edge_id`` carries the
      row id so apply can target it directly.
    - ``action='resync'``: the key exists and is desired, but its non-key
      columns drifted from canonical values (the ADR-0004 row-ownership
      invariant). Apply re-canonicalises via ``EdgeStore.upsert``. ``edge_id``
      carries the row id for log diagnostics.
    """

    target: Literal["projection_edge"]
    action: Literal["create", "remove", "resync"]
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
    resynced: int
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

    # Canonical column values for a frontmatter-provenanced row. The
    # projection owns these columns (ADR-0004 row-ownership invariant);
    # any deviation is drift that ``_apply_reconcile`` re-canonicalises.
    _CANONICAL_WEIGHT: float = 1.0
    _CANONICAL_PROVENANCE_ACTOR: str | None = None
    _CANONICAL_EVIDENCE: str | None = None
    _CANONICAL_CONFLICT_STATE: str | None = None

    def __init__(
        self,
        config: LithosConfig | None = None,
        *,
        edge_store: EdgeStore | None = None,
    ) -> None:
        self._config = config
        # Accept an injected store (ADR-0006 Slice 1, issue #263) so the same
        # ``EdgeStore`` instance backs both this projection and
        # ``CorpusIntake.assert_edge``. Fall back to constructing one when
        # callers (e.g. legacy tests) don't inject — preserves the prior
        # contract that ``create()`` is enough to obtain a working
        # projection.
        self._edge_store: EdgeStore = edge_store or EdgeStore(config)

    @classmethod
    async def create(
        cls,
        config: LithosConfig | None = None,
        *,
        edge_store: EdgeStore | None = None,
    ) -> ProvenanceProjection:
        """Construct the projection and open its underlying store eagerly.

        When *edge_store* is supplied the projection adopts it without
        re-opening — the caller is expected to manage the store's lifecycle
        (open / close). When omitted, the projection constructs its own
        store and opens it (legacy path).
        """
        projection = cls(config, edge_store=edge_store)
        if edge_store is None:
            await projection._edge_store.open()
        return projection

    async def close(self) -> None:
        """Close the underlying store. Idempotent.

        When an external ``edge_store`` was injected the caller owns the
        store's lifecycle and should close it themselves; calling
        ``close`` here is still safe (``EdgeStore.close`` is idempotent)
        but does not coordinate with other holders of the same handle.
        """
        await self._edge_store.close()

    @property
    def edge_store(self) -> EdgeStore:
        """The underlying edge store (public accessor promised to CognitiveMemory).

        ADR-0005 anticipated this so reinforcement/consolidation paths stop
        reaching through the private ``_edge_store`` attribute. Corpus-derived
        writes still go through the plan/apply pair; asserted-edge writes through
        ``CorpusIntake.assert_edge``. This handle serves the LCMA write paths that
        mutate rows the projection does not own (``related_to`` reinforcement,
        conflict-resolution updates, edge weakening).
        """
        return self._edge_store

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
        return self._plan_actions(desired, raw, len(snapshot))

    async def _plan_reconcile_node(
        self, node_id: str, sources: list[str], namespace: str
    ) -> ProvenancePlan:
        """Plan the ``derived_from`` reconcile for a single node.

        The per-node analogue of :meth:`_plan_reconcile_to`: reads only the rows
        with ``from_id == node_id`` and diffs them against the node's frontmatter
        *sources*. An empty *sources* (a deleted node) plans removal of every
        ``derived_from`` edge from the node. Same asserted-key-collision blocking
        and column-drift resync as the corpus-wide planner.
        """
        if not self._edge_store.db_path.exists():
            return ProvenancePlan(actions=(), scanned=1, supported=False)

        desired = {(node_id, source_id, namespace) for source_id in sources}
        raw = await self._edge_store.list_edges(
            from_id=node_id, edge_type=self._CORPUS_DERIVED_EDGE_TYPE
        )
        return self._plan_actions(desired, raw, 1)

    def _plan_actions(
        self,
        desired: set[tuple[str, str, str]],
        raw: list[dict[str, object]],
        scanned: int,
    ) -> ProvenancePlan:
        """Diff *desired* frontmatter edges against *raw* existing rows.

        Shared core of :meth:`_plan_reconcile_to` and
        :meth:`_plan_reconcile_node`: partitions raw rows into frontmatter-owned
        vs asserted, then computes create / remove / resync actions, skipping any
        desired key whose slot is held by an asserted row (creating it would
        clobber the asserted row via the UNIQUE-key upsert; ADR-0004).
        """
        existing_map: dict[tuple[str, str, str], str] = {}
        # Track stale-column rows so apply can re-canonicalise them
        # (ADR-0004 row-ownership invariant).
        drifted_keys: set[tuple[str, str, str]] = set()
        # Asserted rows (outside the predicate) that share a natural key with
        # the projection. The schema is UNIQUE on (from_id, to_id, type,
        # namespace), so the asserted row blocks the projection from
        # materialising; such rows survive reconcile untouched.
        asserted_keys: set[tuple[str, str, str]] = set()
        for e in raw:
            key = (str(e["from_id"]), str(e["to_id"]), str(e["namespace"]))
            if e["provenance_type"] == self._CORPUS_DERIVED_PROVENANCE_TYPE:
                existing_map[key] = str(e["edge_id"])
                if self._has_column_drift(e):
                    drifted_keys.add(key)
            else:
                asserted_keys.add(key)

        existing_keys = set(existing_map.keys())
        to_create = desired - existing_keys - asserted_keys
        blocked = desired & asserted_keys
        if blocked:
            logger.warning(
                "ProvenanceProjection: %d frontmatter-declared edge(s) blocked "
                "by asserted rows at the same natural key; asserted edges "
                "survive untouched per ADR-0004",
                len(blocked),
                extra={"blocked_keys": sorted(blocked)},
            )
        to_remove = existing_keys - desired
        # Only resync rows that are both desired and drifted; orphans are
        # removed, not resynced.
        to_resync = (desired & existing_keys) & drifted_keys

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
        for key in sorted(to_resync):
            from_id, to_id, ns = key
            actions.append(
                ProvenanceReconcileAction(
                    target="projection_edge",
                    action="resync",
                    from_id=from_id,
                    to_id=to_id,
                    namespace=ns,
                    edge_id=existing_map[key],
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

        return ProvenancePlan(actions=tuple(actions), scanned=scanned, supported=True)

    def _has_column_drift(self, row: dict[str, object]) -> bool:
        """True when *row* deviates from canonical column values.

        Compares the non-key columns the projection owns
        (``weight``, ``provenance_actor``, ``evidence``, ``conflict_state``)
        against their canonical values for a frontmatter-provenanced edge.
        """
        return (
            row.get("weight") != self._CANONICAL_WEIGHT
            or row.get("provenance_actor") != self._CANONICAL_PROVENANCE_ACTOR
            or row.get("evidence") != self._CANONICAL_EVIDENCE
            or row.get("conflict_state") != self._CANONICAL_CONFLICT_STATE
        )

    async def _apply_reconcile(self, plan: ProvenancePlan) -> ProvenanceResult:
        """Execute *plan* against the projection store. Idempotent.

        ``create`` and ``resync`` actions both route through
        :meth:`EdgeStore.upsert` with canonical column values — the same
        SQL path handles insert and full-column rewrite, so applying a
        ``resync`` re-canonicalises ``weight``, ``provenance_actor``,
        ``evidence``, and ``conflict_state`` for rows that drifted from
        the ADR-0004 invariant.
        """
        if not plan.supported:
            return ProvenanceResult(
                actions=(),
                created=0,
                removed=0,
                resynced=0,
                failed=(),
                scanned=plan.scanned,
                supported=False,
            )

        created = 0
        removed = 0
        resynced = 0
        failures: list[ProvenanceReconcileFailure] = []
        remove_edge_ids: list[str] = []

        for act in plan.actions:
            if act.action in ("create", "resync"):
                try:
                    await self._edge_store.upsert(
                        from_id=act.from_id,
                        to_id=act.to_id,
                        edge_type=self._CORPUS_DERIVED_EDGE_TYPE,
                        weight=self._CANONICAL_WEIGHT,
                        namespace=act.namespace,
                        provenance_actor=self._CANONICAL_PROVENANCE_ACTOR,
                        provenance_type=self._CORPUS_DERIVED_PROVENANCE_TYPE,
                        evidence=self._CANONICAL_EVIDENCE,
                        conflict_state=self._CANONICAL_CONFLICT_STATE,
                    )
                    if act.action == "create":
                        created += 1
                    else:
                        resynced += 1
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
            resynced=resynced,
            failed=tuple(failures),
            scanned=plan.scanned,
            supported=True,
        )

    # ---- public reconcile entry points (ADR-0004) ----
    #
    # ``KnowledgeManager.plan_reconcile`` / ``apply_reconcile`` drive the private
    # pair above during whole-pipeline Reconcile. These public wrappers let the
    # enrich worker reconcile provenance edges on its own cadence (per-node on
    # drain, corpus-wide on sweep) without reaching through to private methods or
    # re-implementing the canonical-row / asserted-key logic (task 681ac952 PR1c).

    async def reconcile_corpus(self, docs: Iterable[KnowledgeDocument]) -> ProvenanceResult:
        """Reconcile every ``derived_from`` edge against *docs*. Idempotent."""
        plan = await self._plan_reconcile_to(docs)
        return await self._apply_reconcile(plan)

    async def reconcile_node(
        self, node_id: str, *, sources: list[str], namespace: str
    ) -> ProvenanceResult:
        """Reconcile one node's ``derived_from`` edges against *sources*.

        *sources* is the node's frontmatter ``derived_from_ids`` (empty for a
        deleted node → every ``derived_from`` edge from the node is removed).
        Callers pass pre-resolved ``sources`` / ``namespace`` so the projection
        stays KnowledgeManager-decoupled; the projection owns the canonical row,
        the asserted-key-collision blocking, and the column-drift resync.
        """
        plan = await self._plan_reconcile_node(node_id, sources, namespace)
        return await self._apply_reconcile(plan)

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
