"""Conformance test suite for the link Graph derived view.

The Graph is one of Reconcile's three symmetric derived views (Search, Graph,
Provenance). Provenance and freshness already have conformance suites; this
fills the gap for the corpus-wiki-links->graph projection (task 87b45d1f,
item 11e).

Invariants proven here:

- Node set == corpus: every written doc becomes a graph node and nothing else.
- Links become edges with backlinks: a ``[[target]]`` in A's content produces
  an outgoing A->B edge and a symmetric incoming B<-A backlink.
- resolve_link matches the corpus: a resolvable target resolves to its doc id.
- Unresolved links are not fabricated: a dangling ``[[...]]`` creates no
  phantom node or edge and does not resolve.
- Idempotent convergence: once the derived view is reconciled to the corpus,
  re-planning finds no drift, and applying again leaves it converged.

Note on wiki-link form: ``resolve_link`` resolves by path / filename-slug /
uuid / alias, not by a title-with-spaces. Notes here use the slug form
(``[[beta-note]]`` for the note titled "Beta Note", stored at ``beta-note.md``)
so links resolve deterministically regardless of write order.
"""

import pytest

from lithos.server import LithosServer
from tests.helpers import call_tool

pytestmark = pytest.mark.integration


async def _write(server: LithosServer, title: str, content: str) -> str:
    """Write a note through the MCP path and return its id."""
    result = await call_tool(
        server,
        "lithos_write",
        {"title": title, "content": content, "agent": "conf-agent"},
    )
    assert result["status"] == "created"
    return result["id"]


class TestGraphNodeSetConformance:
    """The graph's node set mirrors the corpus exactly."""

    async def test_node_set_equals_corpus(self, server: LithosServer):
        """Every written doc is a node; no extra nodes exist."""
        ids = {
            await _write(server, "Node One", "First node."),
            await _write(server, "Node Two", "Second node."),
            await _write(server, "Node Three", "Third node."),
        }
        assert server.graph.get_doc_ids() == ids


class TestGraphEdgeConformance:
    """Wiki-links project to directed edges with symmetric backlinks."""

    async def test_links_become_edges_with_backlinks(self, server: LithosServer):
        """A ``[[beta-note]]`` in A yields A->B outgoing and B<-A incoming."""
        b_id = await _write(server, "Beta Note", "Target note.")
        a_id = await _write(server, "Alpha Note", "Alpha references [[beta-note]] inline.")

        outgoing = server.graph.get_links(a_id, direction="outgoing")
        assert b_id in {d.id for d in outgoing.outgoing}

        incoming = server.graph.get_links(b_id, direction="incoming")
        assert a_id in {d.id for d in incoming.incoming}

    async def test_resolve_link_matches_corpus(self, server: LithosServer):
        """A resolvable slug target resolves to the corresponding doc id."""
        b_id = await _write(server, "Beta Note", "Target note.")
        assert server.graph.resolve_link("beta-note") == b_id


class TestGraphUnresolvedConformance:
    """Dangling wiki-links are never fabricated into nodes or edges."""

    async def test_unresolved_link_not_fabricated(self, server: LithosServer):
        """A ``[[ghost-note]]`` with no matching doc creates no phantom.

        resolve_link returns None, the graph's doc-id set stays limited to the
        real corpus, and the source note has no resolved outgoing edge (the
        placeholder is not a real node).
        """
        solo_id = await _write(server, "Solo Note", "Solo mentions [[ghost-note]] which is absent.")

        assert server.graph.resolve_link("ghost-note") is None
        # No phantom node leaked into the corpus node set.
        assert server.graph.get_doc_ids() == {solo_id}
        # The dangling link produced no resolved outgoing edge.
        outgoing = server.graph.get_links(solo_id, direction="outgoing")
        assert outgoing.outgoing == []


class TestGraphReconcileConformance:
    """Corpus-wiki-links->graph derived-view convergence over the seam."""

    async def test_reconcile_converges_and_is_idempotent(self, server: LithosServer):
        """Once reconciled to the corpus, the graph view stays converged.

        Writes update the in-memory graph, but the on-disk graph cache the
        planner reads is debounced (#203) and may lag, so the first plan after
        writes reports drift. Applying it flushes a cache that agrees with the
        corpus; re-planning is then a noop, and applying that noop again leaves
        it converged. Plain notes (no wiki-links) keep the corpus free of
        dangling targets, which would otherwise surface as report-only
        ``stale_link`` actions.
        """
        await _write(server, "Converge One", "Plain body one.")
        await _write(server, "Converge Two", "Plain body two.")
        await _write(server, "Converge Three", "Plain body three.")

        # Bring the derived view into agreement with the corpus.
        plan = await server.knowledge.plan_reconcile(graph=server.graph)
        assert plan.graph is not None
        await server.knowledge.apply_reconcile(plan, graph=server.graph)

        # Converged: re-planning finds no drift.
        converged = await server.knowledge.plan_reconcile(graph=server.graph)
        assert converged.graph is not None
        assert converged.graph.is_noop
        assert converged.graph.actions == ()

        # Idempotent: applying the noop plan and re-planning stays converged.
        await server.knowledge.apply_reconcile(converged, graph=server.graph)
        again = await server.knowledge.plan_reconcile(graph=server.graph)
        assert again.graph is not None
        assert again.graph.is_noop
