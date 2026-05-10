"""ProvenanceProjection — corpus-derived edges as a Module facade.

See docs/adr/0004-provenance-projection-module.md.

This Module wraps the SQLite-backed edge store and exposes a public
**read-only** surface to callers. The store and the projection function
under ``lcma/edges.py`` are package-internal implementation details and
must not be imported from outside this module.

Plan/apply reconciliation lands in a follow-up slice (#254) per ADR-0001
step 3; this file intentionally does not expose them yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lithos.config import LithosConfig

# This module is the single legitimate import site for the package-internal
# edge store and the corpus-to-edges projection function (ADR-0004,
# issue #251). The class and helper stay importable from here as
# **undocumented transitional internals** — they are deliberately NOT
# listed in ``__all__`` because the public surface of this slice is
# ``ProvenanceProjection`` only. Callers that still need a direct
# reference (type annotations in ``server.py`` / ``lcma/retrieve.py``,
# fixture construction in tests, and the un-migrated helpers in
# ``lcma/`` and ``reconcile.py``) should treat them as private until
# their dedicated follow-ups land (#254 / #255 / #258 / #263). The
# import-graph guard in #262 will enforce the rule mechanically.
from lithos.lcma.edges import (
    EdgeStore,
    _project_node_provenance,  # noqa: F401  re-exported for transitional callers
    _project_provenance_to_edges,
)

if TYPE_CHECKING:
    from lithos.knowledge import KnowledgeManager

# Public module API — only the façade. The internal symbols above are
# importable via ``from lithos.provenance import EdgeStore`` for the
# transitional callers listed in the comment, but they are not part of
# the documented interface and ``from lithos.provenance import *`` will
# not pick them up.
__all__ = ["ProvenanceProjection"]


class ProvenanceProjection:
    """Module facade over the corpus-derived edge projection.

    Construction follows ADR-0002's eager-init pattern: callers always
    obtain the projection via :meth:`create`, which opens the underlying
    store before returning, so no caller observes a half-initialised
    projection.
    """

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

    # ---- transitional package-internal hook ----

    async def _project(self, knowledge: KnowledgeManager) -> dict[str, int]:
        """Trigger the corpus-to-edges projection against this Module's store.

        **Transitional, package-internal.** This is a thin shim over the
        package-internal projection helper so tests and any future
        in-package callers can drive the projection without importing
        ``_project_provenance_to_edges`` directly. The eventual public
        surface is the plan/apply pair described in ADR-0004
        (``_plan_reconcile_to`` / ``_apply_reconcile``), which lands in
        issue #254 alongside ``KnowledgeManager.apply_reconcile``
        dispatch. This method is expected to be removed at that point.

        Returns the ``{"created": N, "removed": M}`` dict produced by the
        underlying helper.
        """
        return await _project_provenance_to_edges(self._edge_store, knowledge)

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
