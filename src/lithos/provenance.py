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

from lithos.config import LithosConfig

# This module is the single legitimate import site for the package-internal
# edge store and the corpus-to-edges projection function (ADR-0004,
# issue #251). Re-export them here so callers that still need a direct
# reference (type annotations, transitional tests, the package-internal
# helpers in lcma/ that #254 / #255 have not yet migrated) import them
# via ``lithos.provenance`` instead of reaching into ``lithos.lcma.edges``
# directly. The import-graph guard in #262 will enforce this rule
# mechanically.
from lithos.lcma.edges import EdgeStore, _project_provenance_to_edges

__all__ = [
    "EdgeStore",
    "ProvenanceProjection",
    "_project_provenance_to_edges",
]


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
