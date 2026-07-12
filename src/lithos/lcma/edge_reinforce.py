"""Shared reinforcement policy for undirected ``related_to`` edges.

Both the enrich worker's task consolidation (``EnrichWorker._consolidate_task``)
and CognitiveMemory's citation reinforcement (``reinforce_between``) strengthen
or create a canonical ``related_to`` edge between a pair of same-namespace nodes.
The create-or-strengthen mechanics were duplicated; this module owns the single
copy. Callers keep their own concerns — pair enumeration, canonical
``from_id <= to_id`` ordering, same-namespace filtering, and any cross-store
idempotency bookkeeping (they differ only in the initial weight and
``provenance_type`` recorded for a brand-new edge).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lithos.edge_store import EdgeStore

# Weight bump applied to an already-existing related_to edge when a pair is
# reinforced again. Shared by both callers (was hard-coded to 0.03 in each).
RELATED_TO_STRENGTHEN_DELTA = 0.03


async def reinforce_related_edge(
    edge_store: EdgeStore,
    from_id: str,
    to_id: str,
    namespace: str,
    *,
    initial_weight: float,
    provenance_type: str,
    strengthen_delta: float = RELATED_TO_STRENGTHEN_DELTA,
) -> tuple[str, bool]:
    """Strengthen an existing ``related_to`` edge or create one.

    If a ``related_to`` edge already exists for ``(from_id, to_id, namespace)``
    its weight is bumped by ``strengthen_delta``; otherwise a new edge is created
    at ``initial_weight`` with ``provenance_type``.

    Returns ``(edge_id, created)`` — ``created`` is ``True`` when a new edge was
    inserted, ``False`` when an existing edge was strengthened — so callers can
    keep their own create-vs-strengthen logging.

    The caller is responsible for canonicalising ``from_id <= to_id`` and for
    same-namespace filtering; this helper neither reorders nor validates.
    """
    existing = await edge_store.list_edges(
        from_id=from_id, to_id=to_id, edge_type="related_to", namespace=namespace
    )
    if existing:
        edge_id = str(existing[0]["edge_id"])
        await edge_store.adjust_weight(edge_id, strengthen_delta)
        return edge_id, False
    edge_id = await edge_store.upsert(
        from_id=from_id,
        to_id=to_id,
        edge_type="related_to",
        weight=initial_weight,
        namespace=namespace,
        provenance_type=provenance_type,
    )
    return edge_id, True
