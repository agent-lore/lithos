"""LCMA edge projection helpers — corpus-derived edge sync against EdgeStore.

The :class:`EdgeStore` itself now lives at :mod:`lithos.edge_store` (ADR-0006
Slice 1, issue #263). This module retains the corpus-to-edges projection
helpers — :func:`_project_node_provenance` and
:func:`_project_provenance_to_edges` — which are still consumed by the
enrich worker's full sweep and by :mod:`lithos.provenance` as transitional
re-exports.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from lithos.edge_store import EdgeStore

if TYPE_CHECKING:
    from lithos.knowledge import KnowledgeManager

logger = logging.getLogger(__name__)

__all__ = [
    "EdgeStore",
    "_project_node_provenance",
    "_project_provenance_to_edges",
]


async def _project_node_provenance(
    edge_store: EdgeStore,
    knowledge: KnowledgeManager,
    node_id: str,
) -> dict[str, int]:
    """Project provenance for a single node into edges.db.

    When the node exists in ``knowledge``:
      - Reads ``derived_from_ids`` via ``get_doc_sources(node_id)``
      - Upserts ``type='derived_from'`` edges for each source
      - Removes orphan ``derived_from`` edges that no longer match frontmatter

    When the node is absent (deleted):
      - Deletes all ``derived_from`` edges where ``from_id == node_id``

    Returns ``{"created": N, "removed": M}`` summarising the delta.
    """
    if not edge_store.db_path.exists():
        return {"created": 0, "removed": 0}

    # Read existing derived_from edges where from_id == node_id
    existing_edges = await edge_store.list_edges(from_id=node_id, edge_type="derived_from")
    existing_map: dict[tuple[str, str], str] = {}
    for e in existing_edges:
        key = (str(e["to_id"]), str(e["namespace"]))
        existing_map[key] = str(e["edge_id"])

    # Node absent — remove all derived_from edges
    if not knowledge.has_document(node_id):
        if existing_edges:
            edge_ids = [str(e["edge_id"]) for e in existing_edges]
            await edge_store.delete_edges(edge_ids=edge_ids)
        return {"created": 0, "removed": len(existing_edges)}

    # Node exists — build desired set
    sources = knowledge.get_doc_sources(node_id)
    cached = knowledge.get_cached_meta(node_id)
    ns = cached.namespace if cached else "default"

    desired: set[tuple[str, str]] = set()
    for source_id in sources:
        desired.add((source_id, ns))

    existing_keys = set(existing_map.keys())
    to_create = desired - existing_keys
    to_remove = existing_keys - desired

    # Upsert all desired edges (not just new ones) so that stale metadata
    # on pre-existing edges is resynced to canonical values.
    for to_id, namespace in desired:
        await edge_store.upsert(
            from_id=node_id,
            to_id=to_id,
            edge_type="derived_from",
            weight=1.0,
            namespace=namespace,
            provenance_type="frontmatter",
        )

    orphan_ids = [existing_map[k] for k in to_remove]
    if orphan_ids:
        await edge_store.delete_edges(edge_ids=orphan_ids)

    return {"created": len(to_create), "removed": len(to_remove)}


async def _project_provenance_to_edges(
    edge_store: EdgeStore,
    knowledge: KnowledgeManager,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Project ``derived_from_ids`` from frontmatter into edges.db.

    For every document that has ``derived_from_ids``, ensures a
    ``type='derived_from'`` edge exists from the document to each source.
    Removes orphan ``derived_from`` edges that no longer correspond to any
    document's frontmatter.

    Returns ``{"created": N, "removed": M}`` summarising the planned (or
    applied) delta. When ``dry_run=True`` the same diff is computed but no
    inserts or deletes are issued — useful for the reconcile dry-run path.

    No-op (returns ``{"created": 0, "removed": 0}``) when edges.db does not
    exist on disk.
    """
    if not edge_store.db_path.exists():
        return {"created": 0, "removed": 0}

    # Build the desired set of (from_id, to_id, namespace) from frontmatter.
    # Namespace comes from the metadata cache so explicit frontmatter
    # overrides are honored — never re-derived from path here.
    desired: set[tuple[str, str, str]] = set()
    for doc_id, sources in knowledge.iter_doc_sources():
        if not sources:
            continue
        cached = knowledge.get_cached_meta(doc_id)
        ns = cached.namespace if cached else "default"
        for source_id in sources:
            desired.add((doc_id, source_id, ns))

    # Read existing derived_from edges from edges.db
    existing_edges = await edge_store.list_edges(edge_type="derived_from")
    existing_map: dict[tuple[str, str, str], str] = {}
    for e in existing_edges:
        key = (str(e["from_id"]), str(e["to_id"]), str(e["namespace"]))
        existing_map[key] = str(e["edge_id"])

    existing_keys = set(existing_map.keys())

    # Compute the diff. Dry-run reports planned counts without writing.
    to_create = desired - existing_keys
    to_remove = existing_keys - desired
    created_count = len(to_create)
    removed_count = len(to_remove)

    if dry_run:
        logger.info(
            "provenance projection dry_run: to_create=%d to_remove=%d",
            created_count,
            removed_count,
            extra={"to_create": created_count, "to_remove": removed_count, "dry_run": True},
        )
        return {"created": created_count, "removed": removed_count}

    # Apply the create side of the diff.
    for from_id, to_id, ns in to_create:
        await edge_store.upsert(
            from_id=from_id,
            to_id=to_id,
            edge_type="derived_from",
            weight=1.0,
            namespace=ns,
            provenance_type="frontmatter",
        )

    # Apply the remove side; trust the planned count rather than re-counting.
    orphan_ids = [existing_map[k] for k in to_remove]
    if orphan_ids:
        await edge_store.delete_edges(edge_ids=orphan_ids)

    logger.info(
        "provenance projection applied: created=%d removed=%d",
        created_count,
        removed_count,
        extra={"edges_created": created_count, "edges_removed": removed_count, "dry_run": False},
    )
    return {"created": created_count, "removed": removed_count}
