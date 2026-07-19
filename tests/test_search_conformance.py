"""Conformance test suite for the Search engine derived view.

Search is one of Reconcile's three symmetric derived views (Search, Graph,
Provenance). Provenance and freshness already have conformance suites; this
fills the gap for the corpus->index projection (task 87b45d1f, item 11e).

Invariants proven here:

- Convergence: a corpus written through the normal path leaves the search
  index already in agreement — reconcile finds no doc-set drift.
- Indexed-set == corpus: both search backends index exactly the written docs.
- Idempotent apply: applying a reconcile repairs drift, and a repaired index
  needs no further action; applying again leaves the same state.
- Round-trip: written content is actually findable through ``lithos_search``.
"""

import pytest

from lithos.server import LithosServer
from tests.helpers import call_tool

pytestmark = pytest.mark.integration


# Distinctive bodies so full-text search can disambiguate the round-trip doc.
_CORPUS = {
    "Distributed Systems Primer": (
        "Consensus, replication, and partition tolerance in distributed systems."
    ),
    "Vector Databases": ("Embeddings and approximate nearest neighbor search in vector databases."),
    "Graph Theory Notes": "Nodes, edges, and traversal algorithms in graph theory.",
}


async def _seed_corpus(server: LithosServer) -> set[str]:
    """Write ``_CORPUS`` through the MCP write path and return the doc ids.

    ``lithos_write`` persists the note AND indexes it through the normal
    corpus->index projection, so on return the search backends should already
    agree with the corpus.
    """
    ids: set[str] = set()
    for title, content in _CORPUS.items():
        result = await call_tool(
            server,
            "lithos_write",
            {"title": title, "content": content, "agent": "conf-agent"},
        )
        assert result["status"] == "created"
        ids.add(result["id"])
    return ids


class TestSearchReconcileConformance:
    """Corpus->index derived-view invariants over the reconcile seam."""

    async def test_written_corpus_needs_no_search_rebuild(self, server: LithosServer):
        """A freshly-written corpus is already converged — no doc-set drift.

        The server's one-shot startup schema rebuild leaves Tantivy's
        ``needs_rebuild`` flag set (it is not cleared by the rebuild path), so a
        raw plan always carries a ``schema_mismatch`` action. That flag is a
        startup concern orthogonal to the write-path convergence invariant; the
        existing reconcile unit tests (``test_search_reconcile.py``) flatten it
        the same way before asserting doc-set state.
        """
        await _seed_corpus(server)
        server.search.mark_needs_rebuild(False)

        plan = await server.knowledge.plan_reconcile(search=server.search)

        assert plan.search is not None
        assert plan.search.is_noop
        assert plan.search.actions == ()

    async def test_indexed_set_equals_corpus(self, server: LithosServer):
        """Both search backends index exactly the set of written doc ids.

        These are the same backend accessors ``plan_reconcile_to`` compares
        against the corpus, so the equality here is what makes convergence hold.
        """
        ids = await _seed_corpus(server)

        assert server.search._tantivy.get_indexed_doc_ids() == ids
        assert server.search._chroma.get_indexed_doc_ids() == ids

    async def test_apply_repairs_drift_and_is_idempotent(self, server: LithosServer):
        """apply_reconcile repairs a drifted index; a repaired index converges.

        Drift is made real (not just the schema flag) by clearing both
        backends, so the plan reports ``doc_set_mismatch``. After applying, the
        index matches the corpus and re-planning is a noop — applying again
        leaves the same state.
        """
        ids = await _seed_corpus(server)
        # Flatten the startup schema flag so drift is attributable to doc sets.
        server.search.mark_needs_rebuild(False)
        # Establish genuine drift: indices emptied, corpus still holds N docs.
        server.search.clear_all()

        drift_plan = await server.knowledge.plan_reconcile(search=server.search)
        assert drift_plan.search is not None
        assert not drift_plan.search.is_noop
        reasons = {a.reason for a in drift_plan.search.actions}
        assert reasons == {"doc_set_mismatch"}

        result = await server.knowledge.apply_reconcile(drift_plan, search=server.search)
        assert result.search is not None
        assert result.search.failed == ()

        # Repaired: index matches corpus and re-planning finds nothing to do.
        converged = await server.knowledge.plan_reconcile(search=server.search)
        assert converged.search is not None
        assert converged.search.is_noop
        assert server.search._tantivy.get_indexed_doc_ids() == ids
        assert server.search._chroma.get_indexed_doc_ids() == ids

        # Idempotent: applying the noop plan changes nothing.
        await server.knowledge.apply_reconcile(converged, search=server.search)
        again = await server.knowledge.plan_reconcile(search=server.search)
        assert again.search is not None
        assert again.search.is_noop
        assert server.search._tantivy.get_indexed_doc_ids() == ids


class TestSearchRoundTripConformance:
    """The corpus->index projection actually makes content findable."""

    async def test_written_note_is_findable(self, server: LithosServer):
        """A written note is returned by ``lithos_search`` for its own content."""
        write = await call_tool(
            server,
            "lithos_write",
            {
                "title": "Photosynthesis Overview",
                "content": "Chloroplasts convert sunlight into chemical energy via photosynthesis.",
                "agent": "conf-agent",
            },
        )
        assert write["status"] == "created"
        doc_id = write["id"]

        result = await call_tool(
            server,
            "lithos_search",
            {"query": "chloroplasts sunlight photosynthesis"},
        )
        hit_ids = {r["id"] for r in result["results"]}
        assert doc_id in hit_ids
