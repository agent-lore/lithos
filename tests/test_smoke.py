"""Smoke test — quick end-to-end sanity check across knowledge, search, and coordination.

Run with:  uv run pytest tests/test_smoke.py -v
"""

import pytest

from lithos.server import LithosServer


@pytest.mark.asyncio
async def test_smoke_write_index_search(server: LithosServer):
    """Create a doc, index it, find it via full-text and semantic search."""
    # Write
    doc = await server.knowledge.create(
        title="Smoke Test Entry",
        content="Lithos is a shared memory substrate for AI agents.",
        agent="smoke-agent",
        tags=["smoke", "test"],
    )
    server.search.index_document(doc)
    server.graph.add_document(doc)

    # Full-text search
    ft_results = server.search.full_text_search("shared memory substrate")
    assert any(r.id == doc.id for r in ft_results), "full-text search should find the doc"

    # Semantic search (use a low threshold — short docs produce weaker similarity)
    sem_results = server.search.semantic_search("knowledge base for AI", threshold=0.2)
    assert any(r.id == doc.id for r in sem_results), "semantic search should find the doc"

    # Read back and verify content round-trips
    read_doc, truncated = await server.knowledge.read(id=doc.id)
    assert read_doc.title == "Smoke Test Entry"
    assert "shared memory substrate" in read_doc.content
    assert not truncated


@pytest.mark.asyncio
async def test_smoke_coordination_lifecycle(server: LithosServer):
    """Create a task, claim it, post a finding, complete it."""
    # Register agent
    success, _created = await server.coordination.register_agent(
        "smoke-agent",
        name="Smoke Agent",
        agent_type="test",
    )
    assert success

    # Create task
    task_id = await server.coordination.create_task(
        title="Smoke Coordination Task",
        agent="smoke-agent",
        description="Verify the coordination lifecycle works end-to-end.",
        tags=["smoke"],
    )
    assert task_id

    # Claim an aspect
    claimed, expires_at = await server.coordination.claim_task(
        task_id=task_id,
        aspect="verification",
        agent="smoke-agent",
        ttl_minutes=10,
    )
    assert claimed
    assert expires_at is not None

    # Post a finding
    finding_id = await server.coordination.post_finding(
        task_id=task_id,
        agent="smoke-agent",
        summary="Smoke check passed.",
    )
    assert finding_id

    # Verify finding is listed
    findings = await server.coordination.list_findings(task_id)
    assert any(f.id == finding_id for f in findings)

    # Complete the task
    completed = await server.coordination.complete_task(task_id, "smoke-agent")
    assert completed

    # Confirm status
    task = await server.coordination.get_task(task_id)
    assert task.status == "completed"


@pytest.mark.asyncio
async def test_smoke_graph_links(server: LithosServer):
    """Create two linked docs and verify the graph connects them."""
    target = await server.knowledge.create(
        title="Smoke Target",
        content="This document is the link target.",
        agent="smoke-agent",
    )
    server.graph.add_document(target)

    source = await server.knowledge.create(
        title="Smoke Source",
        content="Refers to [[smoke-target]] for details.",
        agent="smoke-agent",
    )
    server.graph.add_document(source)

    # Verify edge exists
    assert server.graph.has_edge(source.id, target.id)

    # Verify incoming link on target
    incoming = server.graph.get_incoming_links(target.id)
    assert any(n["id"] == source.id for n in incoming)
