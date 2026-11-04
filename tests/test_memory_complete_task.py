"""Service-level contract of ``CognitiveMemory.complete_task`` (task 3706b7f7).

The MCP handler ``lithos_task_complete`` used to sequence feedback validation,
the coordination state transition, reinforcement, the ``task.completed`` event
and the unblocked query itself. That ordering is now owned by the memory
facade; these tests pin it without going through the tool layer.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
import pytest_asyncio

from lithos.config import LithosConfig
from lithos.errors import CoordinationError
from lithos.events import EVENT_ORIGIN_ENRICH, TASK_COMPLETED
from lithos.server import LithosServer

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def srv(test_config: LithosConfig) -> LithosServer:
    server = LithosServer(test_config)
    await server.initialize()
    yield server  # type: ignore[misc]
    await server.shutdown()


async def _create_task(server: LithosServer, agent: str = "test-agent", **kw: Any) -> str:
    return await server.coordination.create_task(title="Feedback Task", agent=agent, **kw)


async def _create_notes(server: LithosServer, count: int) -> list[str]:
    ids: list[str] = []
    for i in range(count):
        result = await server.knowledge.create(
            title=f"Note {i}", content=f"Content {i}.", agent="test-agent"
        )
        assert result.document is not None
        ids.append(result.document.id)
    return ids


async def _insert_receipt(
    server: LithosServer, receipt_id: str, task_id: str, agent_id: str, node_ids: list[str]
) -> None:
    await server.memory._stats_store.insert_receipt(
        receipt_id=receipt_id,
        query="q",
        limit=10,
        namespace_filter=None,
        scouts_fired=["scout_vector"],
        candidates_considered=len(node_ids),
        final_nodes=[{"id": nid, "score": 0.9} for nid in node_ids],
        conflicts_surfaced=[],
        surface_conflicts=False,
        temperature=0.5,
        terrace_reached=1,
        agent_id=agent_id,
        task_id=task_id,
    )


async def _cited_count(server: LithosServer, node_id: str) -> int:
    stats = await server.memory._stats_store.get_node_stats(node_id)
    return 0 if stats is None else int(stats["cited_count"])  # type: ignore[arg-type]


async def _status(server: LithosServer, task_id: str) -> str:
    task = await server.coordination.get_task(task_id)
    assert task is not None
    return task.status


class TestValidationBeforeTransition:
    async def test_invalid_receipt_raises_and_leaves_task_open(self, srv: LithosServer) -> None:
        task_id = await _create_task(srv)
        other = await _create_task(srv, agent="other-agent")
        node_ids = await _create_notes(srv, 1)
        await _insert_receipt(srv, "rcpt-other", other, "test-agent", node_ids)
        queue = srv.event_bus.subscribe(event_types=[TASK_COMPLETED])

        with pytest.raises(CoordinationError) as excinfo:
            await srv.memory.complete_task(
                task_id, "test-agent", cited_nodes=node_ids, receipt_id="rcpt-other"
            )

        assert excinfo.value.code == "receipt_not_found"
        assert await _status(srv, task_id) == "open"
        assert queue.empty()
        assert await _cited_count(srv, node_ids[0]) == 0

    async def test_validate_task_feedback_raises_for_unknown_receipt(
        self, srv: LithosServer
    ) -> None:
        task_id = await _create_task(srv)
        with pytest.raises(CoordinationError) as excinfo:
            await srv.memory.validate_task_feedback(
                task_id=task_id,
                agent="test-agent",
                cited_nodes=["x"],
                misleading_nodes=None,
                receipt_id="missing",
            )
        assert excinfo.value.code == "receipt_not_found"
        assert "missing" in excinfo.value.message


class TestReinforcementAfterTransition:
    async def test_closed_task_raises_without_reinforcement(self, srv: LithosServer) -> None:
        task_id = await _create_task(srv)
        node_ids = await _create_notes(srv, 1)
        await _insert_receipt(srv, "rcpt-ok", task_id, "test-agent", node_ids)
        await srv.coordination.complete_task(task_id, "test-agent")
        queue = srv.event_bus.subscribe(event_types=[TASK_COMPLETED])

        with pytest.raises(CoordinationError) as excinfo:
            await srv.memory.complete_task(
                task_id, "test-agent", cited_nodes=node_ids, receipt_id="rcpt-ok"
            )

        assert excinfo.value.code == "task_not_found"
        assert await _cited_count(srv, node_ids[0]) == 0
        assert queue.empty()

    async def test_success_runs_steps_in_order_and_emits(self, srv: LithosServer) -> None:
        task_id = await _create_task(srv)
        node_ids = await _create_notes(srv, 2)
        await _insert_receipt(srv, "rcpt-ord", task_id, "test-agent", node_ids)
        queue = srv.event_bus.subscribe(event_types=[TASK_COMPLETED])
        order: list[str] = []

        def spy(owner: Any, name: str, label: str) -> None:
            original = getattr(owner, name)

            async def wrapped(*args: Any, **kwargs: Any) -> Any:
                order.append(label)
                return await original(*args, **kwargs)

            setattr(owner, name, wrapped)

        spy(srv.memory, "validate_task_feedback", "validate")
        spy(srv.coordination, "complete_task", "complete")
        spy(srv.memory, "apply_task_feedback", "apply")
        spy(srv.event_bus, "emit", "emit")
        spy(srv.coordination, "newly_unblocked_by", "unblocked")

        result = await srv.memory.complete_task(
            task_id,
            "test-agent",
            outcome="done",
            cited_nodes=[node_ids[0]],
            misleading_nodes=[node_ids[1]],
            receipt_id="rcpt-ord",
        )

        assert order == ["validate", "complete", "apply", "emit", "unblocked"]
        assert result.task_id == task_id
        assert result.unblocked == ()
        assert await _cited_count(srv, node_ids[0]) == 1

        event = queue.get_nowait()
        assert event.type == TASK_COMPLETED
        assert event.agent == "test-agent"
        assert event.origin != EVENT_ORIGIN_ENRICH
        assert event.payload == {
            "task_id": task_id,
            "agent": "test-agent",
            "outcome": "done",
            "cited_nodes": json.dumps([node_ids[0]]),
            "misleading_nodes": json.dumps([node_ids[1]]),
            "receipt_id": json.dumps("rcpt-ord"),
            "updated_at": result.updated_at,
        }

    async def test_logs_reinforcement_counts(
        self, srv: LithosServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        task_id = await _create_task(srv)
        node_ids = await _create_notes(srv, 3)
        await _insert_receipt(srv, "rcpt-log", task_id, "test-agent", node_ids)

        with caplog.at_level(logging.INFO, logger="lithos.cognitive_memory"):
            await srv.memory.complete_task(
                task_id, "test-agent", cited_nodes=[node_ids[0]], misleading_nodes=[node_ids[1]]
            )

        record = next(r for r in caplog.records if "reinforcement" in r.message)
        assert (record.cited_count, record.misleading_count, record.ignored_count) == (1, 1, 1)  # type: ignore[attr-defined]


class TestResultShape:
    async def test_echoes_committed_stamp_and_unblocked_dependents(self, srv: LithosServer) -> None:
        a = await _create_task(srv)
        b = await _create_task(srv, depends_on=[a])

        result = await srv.memory.complete_task(a, "test-agent")

        task = await srv.coordination.get_task(a)
        assert task is not None and task.updated_at is not None
        assert result.updated_at == task.updated_at.isoformat()
        assert result.unblocked == (b,)
        assert await _status(srv, b) == "open"

    async def test_no_receipt_skips_reinforcement_but_completes(
        self, srv: LithosServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        task_id = await _create_task(srv)
        node_ids = await _create_notes(srv, 2)

        with caplog.at_level(logging.WARNING):
            result = await srv.memory.complete_task(
                task_id, "test-agent", cited_nodes=node_ids, misleading_nodes=[]
            )

        assert await _status(srv, task_id) == "completed"
        assert result.unblocked == ()
        assert any("dropping all feedback" in r.message.lower() for r in caplog.records)
        for nid in node_ids:
            assert await _cited_count(srv, nid) == 0

    async def test_no_feedback_does_not_touch_memory(self, srv: LithosServer) -> None:
        task_id = await _create_task(srv)
        touched: list[str] = []

        async def boom(*args: Any, **kwargs: Any) -> Any:
            touched.append("validate")
            raise AssertionError("validate_task_feedback must not run without feedback")

        srv.memory.validate_task_feedback = boom  # type: ignore[method-assign]

        result = await srv.memory.complete_task(task_id, "test-agent", outcome="x")

        assert touched == []
        assert await _status(srv, task_id) == "completed"
        assert isinstance(result.updated_at, str)
