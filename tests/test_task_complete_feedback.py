"""Tests for lithos_task_complete feedback parameters (US-006).

Covers receipt validation, reinforcement/penalty application, and the
full retrieve -> complete-with-feedback cycle.
"""

import json
import logging
from typing import Any

import pytest
import pytest_asyncio

from lithos.config import LithosConfig
from lithos.server import LithosServer

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def srv(test_config: LithosConfig) -> LithosServer:
    """Create and initialise a LithosServer for testing."""
    server = LithosServer(test_config)
    await server.initialize()
    yield server  # type: ignore[misc]
    server.stop_file_watcher()


async def _call(server: LithosServer, tool_name: str, **kwargs: Any) -> dict[str, Any]:
    tool = await server.mcp.get_tool(tool_name)
    return await tool.fn(**kwargs)


async def _create_task(server: LithosServer, agent: str = "test-agent") -> str:
    return await server.coordination.create_task(title="Feedback Task", agent=agent)


async def _create_notes(server: LithosServer, count: int = 3) -> list[str]:
    """Create *count* notes and return their IDs."""
    ids: list[str] = []
    for i in range(count):
        result = await server.knowledge.create(
            title=f"Note {i}",
            content=f"Content for note {i}.",
            agent="test-agent",
        )
        assert result.document is not None
        ids.append(result.document.id)
    return ids


async def _insert_receipt(
    server: LithosServer,
    receipt_id: str,
    task_id: str,
    agent_id: str,
    node_ids: list[str],
) -> None:
    """Insert a receipt with given final node IDs."""
    final_nodes = [{"id": nid, "score": 0.9} for nid in node_ids]
    await server.stats_store.insert_receipt(
        receipt_id=receipt_id,
        query="test query",
        limit=10,
        namespace_filter=None,
        scouts_fired=["scout_vector"],
        candidates_considered=len(node_ids),
        final_nodes=final_nodes,
        conflicts_surfaced=[],
        surface_conflicts=False,
        temperature=0.5,
        terrace_reached=1,
        agent_id=agent_id,
        task_id=task_id,
    )


# -- Existing behaviour unchanged -----------------------------------------


class TestTaskCompleteNoFeedback:
    """Calling with no feedback params preserves existing behaviour."""

    @pytest.mark.asyncio
    async def test_no_feedback_params_existing_behaviour(self, srv: LithosServer) -> None:
        task_id = await _create_task(srv)
        result = await _call(srv, "lithos_task_complete", task_id=task_id, agent="test-agent")
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_cited_none_misleading_none_no_learning(self, srv: LithosServer) -> None:
        """cited_nodes=None, misleading_nodes=None -- no learning step, no penalize_ignored."""
        task_id = await _create_task(srv)
        node_ids = await _create_notes(srv, 2)
        await _insert_receipt(srv, "rcpt-1", task_id, "test-agent", node_ids)

        result = await _call(
            srv,
            "lithos_task_complete",
            task_id=task_id,
            agent="test-agent",
            cited_nodes=None,
            misleading_nodes=None,
        )
        assert result == {"success": True}

        # Verify no stats changes -- ignored_count should be 0
        for nid in node_ids:
            stats = await srv.stats_store.get_node_stats(nid)
            if stats is not None:
                assert stats["ignored_count"] == 0
                assert stats["cited_count"] == 0


# -- Receipt validation ---------------------------------------------------


class TestReceiptValidation:
    """Receipt lookup and validation logic."""

    @pytest.mark.asyncio
    async def test_feedback_no_prior_receipt_dropped_with_warning(
        self, srv: LithosServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No receipt found -> all feedback dropped, WARNING logged."""
        task_id = await _create_task(srv)
        node_ids = await _create_notes(srv, 2)

        with caplog.at_level(logging.WARNING):
            result = await _call(
                srv,
                "lithos_task_complete",
                task_id=task_id,
                agent="test-agent",
                cited_nodes=node_ids,
                misleading_nodes=[],
            )

        assert result == {"success": True}
        assert any("dropping all feedback" in r.message.lower() for r in caplog.records)

        # Verify no reinforcement applied
        for nid in node_ids:
            stats = await srv.stats_store.get_node_stats(nid)
            if stats is not None:
                assert stats["cited_count"] == 0

    @pytest.mark.asyncio
    async def test_receipt_id_provided_binds_to_exact_receipt(self, srv: LithosServer) -> None:
        """receipt_id provided -- feedback binds to that exact receipt."""
        task_id = await _create_task(srv)
        node_ids = await _create_notes(srv, 2)
        await _insert_receipt(srv, "rcpt-exact", task_id, "test-agent", node_ids)

        result = await _call(
            srv,
            "lithos_task_complete",
            task_id=task_id,
            agent="test-agent",
            cited_nodes=[node_ids[0]],
            receipt_id="rcpt-exact",
        )
        assert result == {"success": True}

        stats = await srv.stats_store.get_node_stats(node_ids[0])
        assert stats is not None
        assert stats["cited_count"] == 1

    @pytest.mark.asyncio
    async def test_receipt_id_for_different_task_rejected(self, srv: LithosServer) -> None:
        """receipt_id for different task_id -- rejected with error."""
        task_id = await _create_task(srv)
        other_task_id = await _create_task(srv, agent="other-agent")
        node_ids = await _create_notes(srv, 1)
        # Receipt belongs to other_task_id
        await _insert_receipt(srv, "rcpt-other", other_task_id, "test-agent", node_ids)

        result = await _call(
            srv,
            "lithos_task_complete",
            task_id=task_id,
            agent="test-agent",
            cited_nodes=node_ids,
            receipt_id="rcpt-other",
        )
        assert result["status"] == "error"
        assert result["code"] == "receipt_not_found"

    @pytest.mark.asyncio
    async def test_receipt_id_for_different_task_does_not_close_task(
        self, srv: LithosServer
    ) -> None:
        """receipt_id for wrong task_id -- task must remain open (not completed)."""
        task_id = await _create_task(srv)
        other_task_id = await _create_task(srv, agent="other-agent")
        node_ids = await _create_notes(srv, 1)
        await _insert_receipt(srv, "rcpt-other2", other_task_id, "test-agent", node_ids)

        result = await _call(
            srv,
            "lithos_task_complete",
            task_id=task_id,
            agent="test-agent",
            cited_nodes=node_ids,
            receipt_id="rcpt-other2",
        )
        assert result["status"] == "error"

        # The task should still be open -- a second complete call should succeed
        result2 = await _call(srv, "lithos_task_complete", task_id=task_id, agent="test-agent")
        assert result2 == {"success": True}


# -- Feedback intersection ------------------------------------------------


class TestFeedbackIntersection:
    """Feedback IDs are intersected with receipt node IDs."""

    @pytest.mark.asyncio
    async def test_cited_node_not_in_receipt_silently_dropped(self, srv: LithosServer) -> None:
        """cited_nodes containing ID not in receipt is silently dropped."""
        task_id = await _create_task(srv)
        node_ids = await _create_notes(srv, 3)
        # Receipt only contains first two nodes
        await _insert_receipt(srv, "rcpt-partial", task_id, "test-agent", node_ids[:2])

        result = await _call(
            srv,
            "lithos_task_complete",
            task_id=task_id,
            agent="test-agent",
            cited_nodes=[node_ids[0], node_ids[2]],  # node_ids[2] not in receipt
        )
        assert result == {"success": True}

        # node_ids[0] was cited (in receipt)
        stats0 = await srv.stats_store.get_node_stats(node_ids[0])
        assert stats0 is not None
        assert stats0["cited_count"] == 1

        # node_ids[2] was NOT in receipt -- should not be reinforced
        stats2 = await srv.stats_store.get_node_stats(node_ids[2])
        if stats2 is not None:
            assert stats2["cited_count"] == 0

    @pytest.mark.asyncio
    async def test_misleading_node_not_in_receipt_silently_dropped(self, srv: LithosServer) -> None:
        """misleading_nodes containing ID not in receipt is silently dropped."""
        task_id = await _create_task(srv)
        node_ids = await _create_notes(srv, 3)
        await _insert_receipt(srv, "rcpt-partial2", task_id, "test-agent", node_ids[:2])

        result = await _call(
            srv,
            "lithos_task_complete",
            task_id=task_id,
            agent="test-agent",
            misleading_nodes=[node_ids[1], node_ids[2]],  # node_ids[2] not in receipt
        )
        assert result == {"success": True}

        # node_ids[1] was penalized (in receipt)
        stats1 = await srv.stats_store.get_node_stats(node_ids[1])
        assert stats1 is not None
        assert stats1["misleading_count"] == 1

        # node_ids[2] was NOT in receipt -- should not be penalized
        stats2 = await srv.stats_store.get_node_stats(node_ids[2])
        if stats2 is not None:
            assert stats2["misleading_count"] == 0

    @pytest.mark.asyncio
    async def test_feedback_referencing_deleted_note_dropped(self, srv: LithosServer) -> None:
        """Feedback referencing deleted note -- ID dropped before reinforcement."""
        task_id = await _create_task(srv)
        node_ids = await _create_notes(srv, 2)
        await _insert_receipt(srv, "rcpt-del", task_id, "test-agent", node_ids)

        # Delete one note before completing with feedback
        await srv.knowledge.delete(node_ids[1])

        result = await _call(
            srv,
            "lithos_task_complete",
            task_id=task_id,
            agent="test-agent",
            cited_nodes=node_ids,
        )
        assert result == {"success": True}

        # node_ids[0] still exists -- should be cited
        stats0 = await srv.stats_store.get_node_stats(node_ids[0])
        assert stats0 is not None
        assert stats0["cited_count"] == 1

        # node_ids[1] was deleted -- no stats change expected
        stats1 = await srv.stats_store.get_node_stats(node_ids[1])
        if stats1 is not None:
            assert stats1["cited_count"] == 0


# -- Ignored / empty-list semantics ---------------------------------------


class TestIgnoredPenalization:
    """Nodes in receipt but not in cited/misleading get penalize_ignored."""

    @pytest.mark.asyncio
    async def test_empty_lists_all_receipt_nodes_penalized_ignored(self, srv: LithosServer) -> None:
        """cited_nodes=[], misleading_nodes=[] -- learning step runs, all receipt nodes
        passed to penalize_ignored."""
        task_id = await _create_task(srv)
        node_ids = await _create_notes(srv, 2)
        await _insert_receipt(srv, "rcpt-empty", task_id, "test-agent", node_ids)

        result = await _call(
            srv,
            "lithos_task_complete",
            task_id=task_id,
            agent="test-agent",
            cited_nodes=[],
            misleading_nodes=[],
        )
        assert result == {"success": True}

        # All receipt nodes should have ignored_count incremented
        for nid in node_ids:
            stats = await srv.stats_store.get_node_stats(nid)
            assert stats is not None
            assert stats["ignored_count"] == 1


# -- Reinforcement / penalty verification ---------------------------------


class TestReinforcementApplication:
    """Verify actual salience/stat changes from feedback."""

    @pytest.mark.asyncio
    async def test_cited_nodes_salience_increments(self, srv: LithosServer) -> None:
        """call with cited_nodes -- verify salience increments."""
        task_id = await _create_task(srv)
        node_ids = await _create_notes(srv, 2)
        await _insert_receipt(srv, "rcpt-cite", task_id, "test-agent", node_ids)

        result = await _call(
            srv,
            "lithos_task_complete",
            task_id=task_id,
            agent="test-agent",
            cited_nodes=node_ids,
        )
        assert result == {"success": True}

        for nid in node_ids:
            stats = await srv.stats_store.get_node_stats(nid)
            assert stats is not None
            assert stats["cited_count"] == 1
            salience = stats["salience"]
            assert isinstance(salience, float)
            # Default is 0.5, after +0.02 should be 0.52
            assert abs(salience - 0.52) < 0.001

    @pytest.mark.asyncio
    async def test_misleading_nodes_penalties_applied(self, srv: LithosServer) -> None:
        """call with misleading_nodes -- verify penalties applied."""
        task_id = await _create_task(srv)
        node_ids = await _create_notes(srv, 2)
        await _insert_receipt(srv, "rcpt-mislead", task_id, "test-agent", node_ids)

        result = await _call(
            srv,
            "lithos_task_complete",
            task_id=task_id,
            agent="test-agent",
            misleading_nodes=node_ids,
        )
        assert result == {"success": True}

        for nid in node_ids:
            stats = await srv.stats_store.get_node_stats(nid)
            assert stats is not None
            assert stats["misleading_count"] == 1
            salience = stats["salience"]
            assert isinstance(salience, float)
            # Default 0.5, after -0.05 should be 0.45
            assert abs(salience - 0.45) < 0.001


# -- Event payload --------------------------------------------------------


class TestEventPayload:
    """TASK_COMPLETED event includes feedback fields as JSON strings."""

    @pytest.mark.asyncio
    async def test_event_payload_includes_feedback_fields(self, srv: LithosServer) -> None:
        task_id = await _create_task(srv)
        node_ids = await _create_notes(srv, 2)
        await _insert_receipt(srv, "rcpt-evt", task_id, "test-agent", node_ids)

        captured_events: list[Any] = []
        original_emit = srv._emit

        async def capture_emit(event: Any) -> None:
            captured_events.append(event)
            await original_emit(event)

        srv._emit = capture_emit  # type: ignore[assignment]

        await _call(
            srv,
            "lithos_task_complete",
            task_id=task_id,
            agent="test-agent",
            cited_nodes=[node_ids[0]],
            misleading_nodes=[node_ids[1]],
            receipt_id="rcpt-evt",
        )

        # Find TASK_COMPLETED event
        tc_events = [e for e in captured_events if e.type == "task.completed"]
        assert len(tc_events) == 1
        payload = tc_events[0].payload
        assert json.loads(payload["cited_nodes"]) == [node_ids[0]]
        assert json.loads(payload["misleading_nodes"]) == [node_ids[1]]
        assert json.loads(payload["receipt_id"]) == "rcpt-evt"


# -- Integration test: full cycle -----------------------------------------


class TestFullReinforcementCycle:
    """Full cycle: retrieve -> complete with feedback -> verify stats."""

    @pytest.mark.asyncio
    async def test_full_cycle_reinforcement(self, srv: LithosServer) -> None:
        """Retrieve, complete with cited+misleading, verify node-stats changes."""
        # Setup: 3 notes created via lithos_write so they get indexed
        for i in range(3):
            write_result = await _call(
                srv,
                "lithos_write",
                title=f"Note {i}",
                content=f"Content for note {i}.",
                agent="test-agent",
            )
            assert write_result.get("status") != "error", f"lithos_write failed: {write_result}"
        task_id = await _create_task(srv)

        # Perform an actual retrieval to generate a receipt
        retrieve_result = await _call(
            srv,
            "lithos_retrieve",
            query="Content for note",
            limit=10,
            agent_id="test-agent",
            task_id=task_id,
        )
        receipt_id = retrieve_result.get("receipt_id")
        assert receipt_id is not None, "lithos_retrieve must return a receipt_id"

        # Collect the node IDs that were actually returned in the retrieval
        retrieved_ids = [r["id"] for r in retrieve_result.get("results", [])]
        # We need at least 2 nodes to test cited vs misleading vs ignored
        assert len(retrieved_ids) >= 2, f"Expected >=2 retrieved nodes, got {len(retrieved_ids)}"

        # Pick nodes from those actually in the receipt
        cited_id = retrieved_ids[0]
        misleading_id = retrieved_ids[-1] if retrieved_ids[-1] != cited_id else retrieved_ids[1]
        ignored_ids = [nid for nid in retrieved_ids if nid != cited_id and nid != misleading_id]

        # Agent completes: first node was useful, last node was misleading
        result = await _call(
            srv,
            "lithos_task_complete",
            task_id=task_id,
            agent="test-agent",
            cited_nodes=[cited_id],
            misleading_nodes=[misleading_id],
            receipt_id=receipt_id,
        )
        assert result == {"success": True}

        # cited node: cited_count=1, salience ~0.52, spaced_rep_strength ~0.05
        stats_cited = await srv.stats_store.get_node_stats(cited_id)
        assert stats_cited is not None
        assert stats_cited["cited_count"] == 1
        sal0 = stats_cited["salience"]
        assert isinstance(sal0, float)
        assert abs(sal0 - 0.52) < 0.001
        srs0 = stats_cited["spaced_rep_strength"]
        assert isinstance(srs0, float)
        assert abs(srs0 - 0.05) < 0.001

        # ignored nodes: ignored_count=1
        for nid in ignored_ids:
            stats_ign = await srv.stats_store.get_node_stats(nid)
            assert stats_ign is not None
            assert stats_ign["ignored_count"] == 1
            assert stats_ign["cited_count"] == 0
            assert stats_ign["misleading_count"] == 0

        # misleading node: misleading_count=1, salience ~0.45
        stats_mis = await srv.stats_store.get_node_stats(misleading_id)
        assert stats_mis is not None
        assert stats_mis["misleading_count"] == 1
        sal2 = stats_mis["salience"]
        assert isinstance(sal2, float)
        assert abs(sal2 - 0.45) < 0.001
