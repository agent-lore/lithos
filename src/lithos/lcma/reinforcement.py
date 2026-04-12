"""LCMA reinforcement — positive and negative feedback signals.

Positive reinforcement boosts salience and strengthens edges between cited
nodes.  Negative reinforcement decays salience for ignored nodes and
penalises misleading ones.  All operations are atomic (single-row SQL
updates) and safe for concurrent callers.
"""

from __future__ import annotations

import itertools
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lithos.knowledge import KnowledgeManager
    from lithos.lcma.edges import EdgeStore
    from lithos.lcma.stats import StatsStore

logger = logging.getLogger(__name__)

# ── Positive reinforcement ──────────────────────────────────────────────


async def reinforce_cited_nodes(
    cited_ids: list[str],
    edge_store: EdgeStore,
    stats_store: StatsStore,
    knowledge: KnowledgeManager,
) -> None:
    """Boost stats for every cited node.

    For each *cited_id*:
    - ``cited_count += 1``
    - ``salience += 0.02``
    - ``spaced_rep_strength += 0.05``
    """
    for node_id in cited_ids:
        await stats_store.increment_cited(node_id)
        await stats_store.update_salience(node_id, 0.02)
        await stats_store.update_spaced_rep_strength(node_id, 0.05)
        logger.debug("Reinforced cited node %s", node_id)


async def reinforce_edges_between(
    cited_ids: list[str],
    edge_store: EdgeStore,
    knowledge: KnowledgeManager,
) -> None:
    """Strengthen ``related_to`` edges between all cited-node pairs.

    For each unique pair of cited nodes that share a namespace, the
    existing ``related_to`` edge is strengthened by +0.03.  If no edge
    exists, one is created with weight 0.5 (the default salience).

    Pairs are canonicalised so that ``from_id <= to_id`` lexicographically,
    ensuring a single edge record per undirected relationship.

    Cross-namespace pairs are silently skipped.
    """
    # Resolve namespace for each node from the meta cache.
    ns_map: dict[str, str] = {}
    for nid in cited_ids:
        cached = knowledge._meta_cache.get(nid)
        if cached is not None:
            ns_map[nid] = cached.namespace
        else:
            logger.debug("Node %s not in _meta_cache — skipping edge reinforcement", nid)

    # Generate canonical same-namespace pairs.
    for a, b in itertools.combinations(cited_ids, 2):
        ns_a = ns_map.get(a)
        ns_b = ns_map.get(b)
        if ns_a is None or ns_b is None:
            continue
        if ns_a != ns_b:
            logger.debug("Skipping cross-namespace pair (%s, %s): %s != %s", a, b, ns_a, ns_b)
            continue

        # Canonicalise: from_id <= to_id
        from_id, to_id = (a, b) if a <= b else (b, a)
        namespace = ns_a

        # Look for an existing related_to edge between this canonical pair.
        existing = await edge_store.list_edges(
            from_id=from_id, to_id=to_id, edge_type="related_to", namespace=namespace
        )

        if existing:
            edge_id = str(existing[0]["edge_id"])
            await edge_store.adjust_weight(edge_id, 0.03)
            logger.debug("Strengthened edge %s between %s and %s", edge_id, from_id, to_id)
        else:
            eid = await edge_store.upsert(
                from_id=from_id,
                to_id=to_id,
                edge_type="related_to",
                weight=0.5,
                namespace=namespace,
                provenance_type="reinforcement",
            )
            logger.debug("Created related_to edge %s between %s and %s", eid, from_id, to_id)
