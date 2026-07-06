"""Tests for the Phase 1 task graph: edges, ready/blocked, cycles, backfill.

Service-level tests use the ``coordination_service`` fixture; a small set of
server-level tests exercise the new MCP tool envelopes via ``server.mcp.get_tool``.
"""

import json
from typing import Any

import aiosqlite
import pytest

from lithos.config import LithosConfig
from lithos.coordination import CoordinationService
from lithos.errors import CoordinationError
from lithos.server import LithosServer

pytestmark = pytest.mark.asyncio


async def _mk(service: CoordinationService, title: str, agent: str = "a", **kwargs: Any) -> str:
    """Create a task and return its id."""
    return await service.create_task(title=title, agent=agent, **kwargs)


def _ids(tasks: list[dict[str, Any]]) -> set[str]:
    return {t["id"] for t in tasks}


# ==================== Edges: CRUD + validation ====================


class TestTaskEdges:
    async def test_upsert_and_list_roundtrip(self, coordination_service: CoordinationService):
        a = await _mk(coordination_service, "A")
        b = await _mk(coordination_service, "B")

        ok = await coordination_service.upsert_task_edge(a, b, "blocks", "a")
        assert ok is True

        outgoing = await coordination_service.list_task_edges(a, direction="outgoing")
        assert len(outgoing) == 1
        assert outgoing[0]["to_task_id"] == b
        assert outgoing[0]["type"] == "blocks"
        assert outgoing[0]["direction"] == "outgoing"

        incoming = await coordination_service.list_task_edges(b, direction="incoming")
        assert len(incoming) == 1
        assert incoming[0]["from_task_id"] == a
        assert incoming[0]["direction"] == "incoming"

        both = await coordination_service.list_task_edges(a, direction="both")
        assert len(both) == 1

    async def test_upsert_updates_metadata_on_conflict(
        self, coordination_service: CoordinationService
    ):
        a = await _mk(coordination_service, "A")
        b = await _mk(coordination_service, "B")
        await coordination_service.upsert_task_edge(a, b, "blocks", "a", metadata={"v": 1})
        await coordination_service.upsert_task_edge(a, b, "blocks", "a", metadata={"v": 2})

        edges = await coordination_service.list_task_edges(a, direction="outgoing")
        assert len(edges) == 1  # UNIQUE(from,to,type) — upsert, not duplicate
        assert edges[0]["metadata"]["v"] == 2

    async def test_list_edges_type_filter(self, coordination_service: CoordinationService):
        a = await _mk(coordination_service, "A")
        b = await _mk(coordination_service, "B")
        await coordination_service.upsert_task_edge(a, b, "blocks", "a")
        await coordination_service.upsert_task_edge(a, b, "parent_child", "a")

        only_blocks = await coordination_service.list_task_edges(
            a, direction="outgoing", types=["blocks"]
        )
        assert [e["type"] for e in only_blocks] == ["blocks"]

    async def test_self_edge_rejected(self, coordination_service: CoordinationService):
        a = await _mk(coordination_service, "A")
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.upsert_task_edge(a, a, "blocks", "a")
        assert exc.value.code == "self_edge"

    async def test_nonexistent_task_rejected(self, coordination_service: CoordinationService):
        a = await _mk(coordination_service, "A")
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.upsert_task_edge(a, "ghost", "blocks", "a")
        assert exc.value.code == "task_not_found"

    @pytest.mark.parametrize("bad_type", ["duplicate_of", "relates_to", "superseded_by", "nope"])
    async def test_unaccepted_edge_types_rejected(
        self, coordination_service: CoordinationService, bad_type: str
    ):
        a = await _mk(coordination_service, "A")
        b = await _mk(coordination_service, "B")
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.upsert_task_edge(a, b, bad_type, "a")
        assert exc.value.code == "invalid_edge_type"

    async def test_parent_child_is_non_blocking(self, coordination_service: CoordinationService):
        parent = await _mk(coordination_service, "Parent")
        child = await _mk(coordination_service, "Child")
        await coordination_service.upsert_task_edge(parent, child, "parent_child", "a")

        ready = await coordination_service.list_ready()
        # parent_child never blocks: both remain ready
        assert {parent, child} <= _ids(ready)


# ==================== Ready / blocked semantics ====================


class TestReadyBlocked:
    async def test_blocks_until_predecessor_completed(
        self, coordination_service: CoordinationService
    ):
        a = await _mk(coordination_service, "A")
        b = await _mk(coordination_service, "B")
        await coordination_service.upsert_task_edge(a, b, "blocks", "a")

        ready = await coordination_service.list_ready()
        assert a in _ids(ready)
        assert b not in _ids(ready)

        blocked = await coordination_service.list_blocked()
        assert b in _ids(blocked)
        b_row = next(t for t in blocked if t["id"] == b)
        assert b_row["blockers"][0]["kind"] == "task"
        assert b_row["blockers"][0]["task_id"] == a
        assert b_row["blockers"][0]["status"] == "open"

        await coordination_service.complete_task(a, "a")
        ready = await coordination_service.list_ready()
        assert b in _ids(ready)
        assert a not in _ids(ready)  # completed -> off the frontier
        assert b not in _ids(await coordination_service.list_blocked())

    async def test_open_predecessor_keeps_dependent_blocked(
        self, coordination_service: CoordinationService
    ):
        a = await _mk(coordination_service, "A")
        b = await _mk(coordination_service, "B")
        await coordination_service.upsert_task_edge(a, b, "blocks", "a")
        # a is merely open (not completed) -> b is not ready
        assert b not in _ids(await coordination_service.list_ready())

    async def test_cancelled_blocker_is_unsatisfiable(
        self, coordination_service: CoordinationService
    ):
        a = await _mk(coordination_service, "A")
        b = await _mk(coordination_service, "B")
        await coordination_service.upsert_task_edge(a, b, "blocks", "a")
        await coordination_service.cancel_task(a, "a")

        # A cancelled blocker must NOT spuriously ready its dependent.
        assert b not in _ids(await coordination_service.list_ready())

        blocked = await coordination_service.list_blocked()
        b_row = next(t for t in blocked if t["id"] == b)
        blocker = b_row["blockers"][0]
        assert blocker["kind"] == "blocker_unsatisfiable"
        assert blocker["task_id"] == a
        assert blocker["status"] == "cancelled"

    async def test_ready_attaches_claims_never_excludes(
        self, coordination_service: CoordinationService
    ):
        a = await _mk(coordination_service, "A")
        await coordination_service.claim_task(a, aspect="impl", agent="claimer")

        ready = await coordination_service.list_ready(with_claims=True)
        a_row = next(t for t in ready if t["id"] == a)
        # claimed task still appears in the frontier...
        assert a in _ids(ready)
        # ...with its active claim attached
        assert any(c["aspect"] == "impl" for c in a_row["claims"])

    async def test_ready_filters_by_project_metadata(
        self, coordination_service: CoordinationService
    ):
        a = await _mk(coordination_service, "A", metadata={"project": "alpha"})
        b = await _mk(coordination_service, "B", metadata={"project": "beta"})

        alpha = await coordination_service.list_ready(project="alpha")
        assert a in _ids(alpha)
        assert b not in _ids(alpha)

    async def test_ready_respects_limit(self, coordination_service: CoordinationService):
        for i in range(5):
            await _mk(coordination_service, f"T{i}")
        ready = await coordination_service.list_ready(limit=3)
        assert len(ready) == 3

    async def test_ready_limit_with_tags_post_scan(self, coordination_service: CoordinationService):
        # With a tag filter the limit is applied AFTER the Python post-scan, so a
        # SQL LIMIT must NOT be pushed down (it would under-fill). Create more
        # tagged-matching tasks than the limit and confirm we still get `limit`.
        for i in range(5):
            await _mk(coordination_service, f"T{i}", tags=["x"])
        for i in range(3):
            await _mk(coordination_service, f"U{i}", tags=["other"])
        ready = await coordination_service.list_ready(tags=["x"], limit=2)
        assert len(ready) == 2
        assert all("x" in t["tags"] for t in ready)

    async def test_non_positive_limit_returns_empty(
        self, coordination_service: CoordinationService
    ):
        # Non-positive limits must yield no tasks, consistently across the SQL
        # (no-tags) and Python (tags) paths — not one task via append-then-check,
        # and not "all rows" via SQL LIMIT -1.
        for i in range(3):
            await _mk(coordination_service, f"T{i}", tags=["x"])
        assert await coordination_service.list_ready(limit=0) == []
        assert await coordination_service.list_ready(limit=-1) == []
        assert await coordination_service.list_ready(tags=["x"], limit=0) == []
        assert await coordination_service.list_blocked(limit=0) == []


# ==================== Cycles ====================


class TestCycles:
    async def test_direct_cycle_rejected_on_write(self, coordination_service: CoordinationService):
        x = await _mk(coordination_service, "X")
        y = await _mk(coordination_service, "Y")
        await coordination_service.upsert_task_edge(x, y, "blocks", "a")
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.upsert_task_edge(y, x, "blocks", "a")
        assert exc.value.code == "cycle"

    async def test_transitive_cycle_rejected_on_write(
        self, coordination_service: CoordinationService
    ):
        a = await _mk(coordination_service, "A")
        b = await _mk(coordination_service, "B")
        c = await _mk(coordination_service, "C")
        await coordination_service.upsert_task_edge(a, b, "blocks", "a")
        await coordination_service.upsert_task_edge(b, c, "blocks", "a")
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.upsert_task_edge(c, a, "blocks", "a")
        assert exc.value.code == "cycle"

    async def test_parent_child_direct_cycle_rejected(
        self, coordination_service: CoordinationService
    ):
        a = await _mk(coordination_service, "A")
        b = await _mk(coordination_service, "B")
        await coordination_service.upsert_task_edge(a, b, "parent_child", "a")
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.upsert_task_edge(b, a, "parent_child", "a")
        assert exc.value.code == "cycle"

    async def test_parent_child_transitive_cycle_rejected(
        self, coordination_service: CoordinationService
    ):
        a = await _mk(coordination_service, "A")
        b = await _mk(coordination_service, "B")
        c = await _mk(coordination_service, "C")
        await coordination_service.upsert_task_edge(a, b, "parent_child", "a")
        await coordination_service.upsert_task_edge(b, c, "parent_child", "a")
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.upsert_task_edge(c, a, "parent_child", "a")
        assert exc.value.code == "cycle"

    async def test_blocks_and_parent_child_cycle_checks_independent(
        self, coordination_service: CoordinationService
    ):
        # A blocks B and B is A's parent — different relations, no cycle in either.
        a = await _mk(coordination_service, "A")
        b = await _mk(coordination_service, "B")
        await coordination_service.upsert_task_edge(a, b, "blocks", "a")
        await coordination_service.upsert_task_edge(b, a, "parent_child", "a")  # must not raise
        edges = await coordination_service.list_task_edges(a, direction="both")
        assert {(e["from_task_id"], e["type"]) for e in edges} == {
            (a, "blocks"),
            (b, "parent_child"),
        }


# ==================== create_task: depends_on + task_type ====================


class TestCreateConvenience:
    async def test_depends_on_creates_blocks_edges(self, coordination_service: CoordinationService):
        a = await _mk(coordination_service, "A")
        b = await _mk(coordination_service, "B", depends_on=[a])

        incoming = await coordination_service.list_task_edges(b, direction="incoming")
        assert incoming[0]["from_task_id"] == a
        assert incoming[0]["type"] == "blocks"
        assert b not in _ids(await coordination_service.list_ready())

    async def test_depends_on_nonexistent_rejected(self, coordination_service: CoordinationService):
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.create_task(title="B", agent="a", depends_on=["ghost"])
        assert exc.value.code == "task_not_found"

    async def test_invalid_task_type_rejected(self, coordination_service: CoordinationService):
        # task/epic/gate are accepted; 'subtask' was dropped, others are nonsense.
        for bad in ("subtask", "nonsense"):
            with pytest.raises(CoordinationError) as exc:
                await coordination_service.create_task(title="T", agent="a", task_type=bad)
            assert exc.value.code == "invalid_task_type"

    async def test_default_task_type_persisted(self, coordination_service: CoordinationService):
        tid = await _mk(coordination_service, "T")
        task = await coordination_service.get_task(tid)
        assert task is not None
        assert task.task_type == "task"

    async def test_list_tasks_task_type_filter(self, coordination_service: CoordinationService):
        tid = await _mk(coordination_service, "T")
        listed = await coordination_service.list_tasks(task_type="task")
        assert tid in {t["id"] for t in listed}
        assert all(t["task_type"] == "task" for t in listed)


# ==================== Forbidden scheduling metadata ====================


class TestForbiddenMetadata:
    async def test_create_rejects_depends_on_metadata(
        self, coordination_service: CoordinationService
    ):
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.create_task(
                title="T", agent="a", metadata={"depends_on": ["x"]}
            )
        assert exc.value.code == "invalid_metadata_key"

    async def test_update_rejects_blocked_on_metadata(
        self, coordination_service: CoordinationService
    ):
        tid = await _mk(coordination_service, "T")
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.update_task(
                task_id=tid, agent="a", metadata={"blocked_on": ["y"]}
            )
        assert exc.value.code == "invalid_metadata_key"

    async def test_rejects_even_none_delete(self, coordination_service: CoordinationService):
        # presence of the key at all is rejected, even a None delete
        with pytest.raises(CoordinationError):
            await coordination_service.create_task(
                title="T", agent="a", metadata={"depends_on": None}
            )


# ==================== newly_unblocked_by ====================


class TestNewlyUnblocked:
    async def test_completion_unblocks_dependent(self, coordination_service: CoordinationService):
        a = await _mk(coordination_service, "A")
        b = await _mk(coordination_service, "B", depends_on=[a])

        assert await coordination_service.newly_unblocked_by(a) == []  # a still open
        await coordination_service.complete_task(a, "a")
        assert await coordination_service.newly_unblocked_by(a) == [b]

    async def test_multiple_blockers_only_unblocks_when_all_done(
        self, coordination_service: CoordinationService
    ):
        a = await _mk(coordination_service, "A")
        x = await _mk(coordination_service, "X")
        b = await _mk(coordination_service, "B", depends_on=[a, x])

        await coordination_service.complete_task(a, "a")
        assert await coordination_service.newly_unblocked_by(a) == []  # X still blocks
        await coordination_service.complete_task(x, "a")
        assert await coordination_service.newly_unblocked_by(x) == [b]


# ==================== Epic + hierarchy (Phase 2) ====================


class TestEpicAndHierarchy:
    async def test_epic_accepted_and_excluded_from_ready(
        self, coordination_service: CoordinationService
    ):
        epic = await _mk(coordination_service, "Epic", task_type="epic")
        task = await _mk(coordination_service, "Task")
        # epic is a container, not directly workable
        ready_ids = _ids(await coordination_service.list_ready())
        assert task in ready_ids
        assert epic not in ready_ids
        # but it is a real task, filterable by type
        epics = await coordination_service.list_tasks(task_type="epic")
        assert [t["id"] for t in epics] == [epic]

    async def test_parent_task_id_creates_parent_child_edge(
        self, coordination_service: CoordinationService
    ):
        epic = await _mk(coordination_service, "Epic", task_type="epic")
        child = await _mk(coordination_service, "Child", parent_task_id=epic)

        incoming = await coordination_service.list_task_edges(child, direction="incoming")
        assert (incoming[0]["from_task_id"], incoming[0]["type"]) == (epic, "parent_child")
        # parent_child is non-blocking: the child is still ready
        assert child in _ids(await coordination_service.list_ready())

    async def test_single_parent_enforced(self, coordination_service: CoordinationService):
        # Forest invariant: a child may have at most one parent. Adding a second,
        # different parent_child edge to the same child is rejected.
        p1 = await _mk(coordination_service, "P1")
        p2 = await _mk(coordination_service, "P2")
        child = await _mk(coordination_service, "Child", parent_task_id=p1)
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.upsert_task_edge(p2, child, "parent_child", "a")
        assert exc.value.code == "parent_exists"
        # the child still has exactly its original parent
        incoming = await coordination_service.list_task_edges(child, direction="incoming")
        assert [e["from_task_id"] for e in incoming] == [p1]

    async def test_reparent_same_parent_is_idempotent(
        self, coordination_service: CoordinationService
    ):
        # Re-upserting the SAME parent->child edge is allowed (metadata update),
        # not a multi-parent violation.
        p1 = await _mk(coordination_service, "P1")
        child = await _mk(coordination_service, "Child", parent_task_id=p1)
        await coordination_service.upsert_task_edge(
            p1, child, "parent_child", "a", metadata={"note": "x"}
        )
        incoming = await coordination_service.list_task_edges(child, direction="incoming")
        assert len(incoming) == 1
        assert incoming[0]["metadata"]["note"] == "x"

    async def test_parent_may_be_plain_task_unchanged(
        self, coordination_service: CoordinationService
    ):
        parent = await _mk(coordination_service, "Parent")  # a plain task, not an epic
        await _mk(coordination_service, "Child", parent_task_id=parent)
        parent_row = await coordination_service.get_task(parent)
        assert parent_row is not None
        assert parent_row.task_type == "task"  # unchanged by gaining a child

    async def test_parent_task_id_nonexistent_rejected(
        self, coordination_service: CoordinationService
    ):
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.create_task(title="C", agent="a", parent_task_id="ghost")
        assert exc.value.code == "task_not_found"

    async def test_list_children_direct_and_recursive(
        self, coordination_service: CoordinationService
    ):
        epic = await _mk(coordination_service, "Epic", task_type="epic")
        c1 = await _mk(coordination_service, "C1", parent_task_id=epic)
        c2 = await _mk(coordination_service, "C2", parent_task_id=epic)
        g1 = await _mk(coordination_service, "G1", parent_task_id=c1)

        direct = await coordination_service.list_children(epic)
        assert _ids(direct) == {c1, c2}

        deep = await coordination_service.list_children(epic, recursive=True)
        assert _ids(deep) == {c1, c2, g1}

    async def test_list_children_include_closed_filter(
        self, coordination_service: CoordinationService
    ):
        epic = await _mk(coordination_service, "Epic", task_type="epic")
        c1 = await _mk(coordination_service, "C1", parent_task_id=epic)
        g1 = await _mk(coordination_service, "G1", parent_task_id=c1)
        await coordination_service.complete_task(c1, "a")  # close the intermediate

        # default hides the closed child but still surfaces the open grandchild
        open_only = await coordination_service.list_children(epic, recursive=True)
        assert _ids(open_only) == {g1}
        # include_closed surfaces the whole subtree
        everything = await coordination_service.list_children(
            epic, recursive=True, include_closed=True
        )
        assert _ids(everything) == {c1, g1}


# ==================== Spawn (Phase 2) ====================


class TestSpawn:
    async def test_spawn_discovered_from_non_blocking(
        self, coordination_service: CoordinationService
    ):
        source = await _mk(coordination_service, "Source")
        spawned = await coordination_service.spawn_task(source, "Follow-up", "a")

        incoming = await coordination_service.list_task_edges(spawned, direction="incoming")
        assert (incoming[0]["from_task_id"], incoming[0]["type"]) == (source, "discovered_from")
        assert spawned in _ids(await coordination_service.list_ready())  # not blocked

    async def test_spawn_blocks_until_source_completed(
        self, coordination_service: CoordinationService
    ):
        source = await _mk(coordination_service, "Source")
        spawned = await coordination_service.spawn_task(
            source, "Blocked follow-up", "a", relation_type="blocks"
        )
        assert spawned not in _ids(await coordination_service.list_ready())
        await coordination_service.complete_task(source, "a")
        assert spawned in _ids(await coordination_service.list_ready())

    async def test_spawn_invalid_relation_type_rejected(
        self, coordination_service: CoordinationService
    ):
        source = await _mk(coordination_service, "Source")
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.spawn_task(source, "X", "a", relation_type="parent_child")
        assert exc.value.code == "invalid_relation_type"

    async def test_spawn_nonexistent_source_rejected(
        self, coordination_service: CoordinationService
    ):
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.spawn_task("ghost", "X", "a")
        assert exc.value.code == "task_not_found"

    async def test_spawn_inherits_project_tags_and_context(
        self, coordination_service: CoordinationService
    ):
        source = await _mk(
            coordination_service,
            "Source",
            tags=["project:alpha", "area:x"],
            metadata={"project": "alpha", "priority": "high", "phase": "p1", "extra": "no"},
        )
        spawned_id = await coordination_service.spawn_task(source, "Follow-up", "a")
        spawned = await coordination_service.get_task(spawned_id)
        assert spawned is not None
        assert spawned.tags == ["project:alpha", "area:x"]
        assert spawned.metadata["project"] == "alpha"
        # only the scheduling allow-list is inherited as "context"
        assert spawned.metadata["priority"] == "high"
        assert spawned.metadata["phase"] == "p1"
        assert "extra" not in spawned.metadata

    async def test_spawn_explicit_metadata_overrides_inherited(
        self, coordination_service: CoordinationService
    ):
        source = await _mk(coordination_service, "Source", metadata={"priority": "high"})
        spawned_id = await coordination_service.spawn_task(
            source, "Follow-up", "a", metadata={"priority": "low"}
        )
        spawned = await coordination_service.get_task(spawned_id)
        assert spawned is not None
        assert spawned.metadata["priority"] == "low"

    async def test_spawn_rejects_forbidden_metadata(
        self, coordination_service: CoordinationService
    ):
        source = await _mk(coordination_service, "Source")
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.spawn_task(source, "X", "a", metadata={"depends_on": ["y"]})
        assert exc.value.code == "invalid_metadata_key"


# ==================== Gates (Phase 3) ====================

PAST = "2000-01-01T00:00:00+00:00"
FUTURE = "2999-01-01T00:00:00+00:00"


async def _gate(service: CoordinationService, gate_type: str, **md: Any) -> str:
    """Create a gate task of the given gate_type and return its id."""
    return await service.create_task(
        title=f"{gate_type} gate",
        agent="a",
        task_type="gate",
        metadata={"gate_type": gate_type, **md},
    )


class TestGates:
    async def test_gate_accepted_and_excluded_from_ready(
        self, coordination_service: CoordinationService
    ):
        gate = await _gate(coordination_service, "human")
        task = await _mk(coordination_service, "Task")
        ready_ids = _ids(await coordination_service.list_ready())
        assert task in ready_ids
        assert gate not in ready_ids  # a gate is not directly workable

    async def test_gate_validation(self, coordination_service: CoordinationService):
        # missing gate_type
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.create_task(title="G", agent="a", task_type="gate")
        assert exc.value.code == "invalid_input"
        # invalid gate_type
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.create_task(
                title="G", agent="a", task_type="gate", metadata={"gate_type": "bogus"}
            )
        assert exc.value.code == "invalid_input"
        # timer without ready_at
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.create_task(
                title="G", agent="a", task_type="gate", metadata={"gate_type": "timer"}
            )
        assert exc.value.code == "invalid_input"
        # timer with unparseable ready_at
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.create_task(
                title="G",
                agent="a",
                task_type="gate",
                metadata={"gate_type": "timer", "ready_at": "not-a-date"},
            )
        assert exc.value.code == "invalid_input"

    async def test_timer_ready_at_normalized_to_utc(
        self, coordination_service: CoordinationService
    ):
        gate = await _gate(coordination_service, "timer", ready_at="2030-06-01T12:00:00+02:00")
        row = await coordination_service.get_task(gate)
        assert row is not None
        # +02:00 12:00 -> 10:00Z, second-precision, canonical UTC offset
        assert row.metadata["ready_at"] == "2030-06-01T10:00:00+00:00"

    async def test_waits_on_gate_blocks_until_completed(
        self, coordination_service: CoordinationService
    ):
        gate = await _gate(coordination_service, "human")
        task = await _mk(coordination_service, "Task")
        await coordination_service.upsert_task_edge(gate, task, "waits_on_gate", "a")

        assert task not in _ids(await coordination_service.list_ready())
        blocked = await coordination_service.list_blocked()
        b_row = next(t for t in blocked if t["id"] == task)
        assert b_row["blockers"][0]["kind"] == "gate"
        assert b_row["blockers"][0]["task_id"] == gate

        # resolving the gate (completion) readies the waiter and surfaces it
        unblocked = await coordination_service.newly_unblocked_by(gate)
        assert unblocked == []  # not yet completed
        await coordination_service.complete_task(gate, "a")
        assert task in _ids(await coordination_service.list_ready())
        assert await coordination_service.newly_unblocked_by(gate) == [task]

    async def test_cancelled_gate_is_unsatisfiable(self, coordination_service: CoordinationService):
        gate = await _gate(coordination_service, "ci", provider="gha", run_id="1")
        task = await _mk(coordination_service, "Task")
        await coordination_service.upsert_task_edge(gate, task, "waits_on_gate", "a")
        await coordination_service.cancel_task(gate, "a")

        assert task not in _ids(await coordination_service.list_ready())
        blocked = await coordination_service.list_blocked()
        b_row = next(t for t in blocked if t["id"] == task)
        assert b_row["blockers"][0]["kind"] == "blocker_unsatisfiable"
        assert b_row["blockers"][0]["status"] == "cancelled"

    async def test_timer_gate_past_ready_at_auto_resolves(
        self, coordination_service: CoordinationService
    ):
        gate = await _gate(coordination_service, "timer", ready_at=PAST)
        task = await _mk(coordination_service, "Task")
        await coordination_service.upsert_task_edge(gate, task, "waits_on_gate", "a")
        # past ready_at -> resolved at query time, no completion needed
        assert task in _ids(await coordination_service.list_ready())
        assert task not in _ids(await coordination_service.list_blocked())

    async def test_timer_gate_future_ready_at_blocks(
        self, coordination_service: CoordinationService
    ):
        gate = await _gate(coordination_service, "timer", ready_at=FUTURE)
        task = await _mk(coordination_service, "Task")
        await coordination_service.upsert_task_edge(gate, task, "waits_on_gate", "a")
        assert task not in _ids(await coordination_service.list_ready())
        b_row = next(t for t in await coordination_service.list_blocked() if t["id"] == task)
        assert b_row["blockers"][0]["kind"] == "gate"

    async def test_cancelled_timer_past_ready_at_still_unsatisfiable(
        self, coordination_service: CoordinationService
    ):
        # cancelled wins over the timer: a cancelled timer gate past ready_at does
        # NOT resolve its waiter.
        gate = await _gate(coordination_service, "timer", ready_at=PAST)
        task = await _mk(coordination_service, "Task")
        await coordination_service.upsert_task_edge(gate, task, "waits_on_gate", "a")
        await coordination_service.cancel_task(gate, "a")
        assert task not in _ids(await coordination_service.list_ready())
        b_row = next(t for t in await coordination_service.list_blocked() if t["id"] == task)
        assert b_row["blockers"][0]["kind"] == "blocker_unsatisfiable"

    async def test_mixed_blocks_gate_cycle_rejected(
        self, coordination_service: CoordinationService
    ):
        # A blocks G, G waits_on_gate A  ->  A depends on G and G depends on A.
        a = await _mk(coordination_service, "A")
        g = await _gate(coordination_service, "human")
        await coordination_service.upsert_task_edge(a, g, "blocks", "a")
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.upsert_task_edge(g, a, "waits_on_gate", "a")
        assert exc.value.code == "cycle"

    async def test_waits_on_gate_requires_a_gate_blocker(
        self, coordination_service: CoordinationService
    ):
        # The blocker (from) of a waits_on_gate edge must be a gate task, else the
        # readiness predicate can't reason about it and the waiter would leak ready.
        plain = await _mk(coordination_service, "Plain")
        waiter = await _mk(coordination_service, "Waiter")
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.upsert_task_edge(plain, waiter, "waits_on_gate", "a")
        assert exc.value.code == "not_a_gate"

    async def test_update_cannot_strip_gate_invariants(
        self, coordination_service: CoordinationService
    ):
        gate = await _gate(coordination_service, "human")
        task = await _mk(coordination_service, "Task")
        await coordination_service.upsert_task_edge(gate, task, "waits_on_gate", "a")
        assert task not in _ids(await coordination_service.list_ready())

        # deleting gate_type via update is rejected; the waiter stays blocked
        with pytest.raises(CoordinationError) as exc:
            await coordination_service.update_task(gate, "a", metadata={"gate_type": None})
        assert exc.value.code == "invalid_input"
        assert task not in _ids(await coordination_service.list_ready())

        # a valid gate metadata update still works (and normalizes a timer ready_at)
        timer = await _gate(coordination_service, "timer", ready_at=FUTURE)
        ok = await coordination_service.update_task(
            timer, "a", metadata={"ready_at": "2031-01-01T00:00:00Z"}
        )
        assert ok is True
        row = await coordination_service.get_task(timer)
        assert row is not None
        assert row.metadata["ready_at"] == "2031-01-01T00:00:00+00:00"

    async def test_corrupted_gate_metadata_defaults_to_blocked(
        self, coordination_service: CoordinationService
    ):
        # Defense in depth: even if a gate ends up with no gate_type (e.g. a
        # legacy/hand-edited row), the NULL-safe predicate keeps the waiter
        # BLOCKED rather than letting it fall through as ready.
        waiter = await _mk(coordination_service, "Waiter")
        async with aiosqlite.connect(coordination_service.db_path) as db:
            await db.execute(
                "INSERT INTO tasks (id, title, status, task_type, created_by, metadata) "
                "VALUES ('corrupt-gate', 'Corrupt', 'open', 'gate', 'a', '{}')",
            )
            await db.execute(
                "INSERT INTO task_edges (from_task_id, to_task_id, type, created_by) "
                "VALUES ('corrupt-gate', ?, 'waits_on_gate', 'a')",
                (waiter,),
            )
            await db.commit()
        assert waiter not in _ids(await coordination_service.list_ready())
        b_row = next(t for t in await coordination_service.list_blocked() if t["id"] == waiter)
        assert b_row["blockers"][0]["kind"] == "gate"


# ==================== Reopen + cascade (#243) ====================


def _blocker(rows, tid):
    row = next((t for t in rows if t["id"] == tid), None)
    return row["blockers"][0] if row and row.get("blockers") else None


class TestReopen:
    async def test_reopen_completed_blocker_reblocks_dependent(
        self, coordination_service: CoordinationService
    ):
        a = await _mk(coordination_service, "A")
        b = await _mk(coordination_service, "B", depends_on=[a])
        await coordination_service.complete_task(a, "a")
        assert b in _ids(await coordination_service.list_ready())  # A done -> B ready

        prior, _ = await coordination_service.reopen_task(a, "a")
        assert prior == "completed"
        reblocked = await coordination_service.newly_reblocked_by(a, prior)
        assert reblocked == [b]
        assert b not in _ids(await coordination_service.list_ready())  # B blocked again
        assert _blocker(await coordination_service.list_blocked(), b)["kind"] == "task"

    async def test_reopen_cancelled_blocker_unstrands_without_reblock(
        self, coordination_service: CoordinationService
    ):
        a = await _mk(coordination_service, "A")
        b = await _mk(coordination_service, "B", depends_on=[a])
        await coordination_service.cancel_task(a, "a")
        # B is stranded: blocker_unsatisfiable
        assert (
            _blocker(await coordination_service.list_blocked(), b)["kind"]
            == "blocker_unsatisfiable"
        )

        prior, _ = await coordination_service.reopen_task(a, "a")
        assert prior == "cancelled"
        # cancelled-reopen newly-blocks no one (B was already blocked)
        assert await coordination_service.newly_reblocked_by(a, prior) == []
        # but B is now recoverable: blocker_unsatisfiable -> waiting (kind=task), still not ready
        assert b not in _ids(await coordination_service.list_ready())
        assert _blocker(await coordination_service.list_blocked(), b)["kind"] == "task"

    async def test_reopen_completed_gate_reblocks_waiter(
        self, coordination_service: CoordinationService
    ):
        gate = await _gate(coordination_service, "human")
        w = await _mk(coordination_service, "W")
        await coordination_service.upsert_task_edge(gate, w, "waits_on_gate", "a")
        await coordination_service.complete_task(gate, "a")
        assert w in _ids(await coordination_service.list_ready())

        prior, _ = await coordination_service.reopen_task(gate, "a")
        assert await coordination_service.newly_reblocked_by(gate, prior) == [w]
        assert _blocker(await coordination_service.list_blocked(), w)["kind"] == "gate"

    async def test_reblocked_excludes_dependent_with_other_blocker(
        self, coordination_service: CoordinationService
    ):
        # B depends on both A and X. Completing only A never readied B, so reopening
        # A must NOT report B as reblocked (B was already blocked by X).
        a = await _mk(coordination_service, "A")
        x = await _mk(coordination_service, "X")
        b = await _mk(coordination_service, "B", depends_on=[a, x])
        await coordination_service.complete_task(a, "a")
        assert b not in _ids(await coordination_service.list_ready())  # X still blocks
        prior, _ = await coordination_service.reopen_task(a, "a")
        assert await coordination_service.newly_reblocked_by(a, prior) == []

    async def test_reblocked_excludes_terminal_dependent(
        self, coordination_service: CoordinationService
    ):
        # A blocks B; both complete; reopen A. B is terminal (completed), not active
        # work, so it must NOT appear in reblocked even though A is its only blocker.
        a = await _mk(coordination_service, "A")
        b = await _mk(coordination_service, "B", depends_on=[a])
        await coordination_service.complete_task(a, "a")
        await coordination_service.complete_task(b, "b")
        prior, _ = await coordination_service.reopen_task(a, "a")
        assert prior == "completed"
        assert await coordination_service.newly_reblocked_by(a, prior) == []


# ==================== Backfill + migration ====================


class TestBackfillMigration:
    async def _legacy_db(self, db_path, rows: list[tuple[str, dict | None]]) -> None:
        """Create a pre-task_type tasks table and insert (id, metadata) rows."""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT,
                    status TEXT DEFAULT 'open', created_by TEXT NOT NULL,
                    created_at TIMESTAMP, tags JSON, outcome TEXT,
                    resolved_at TIMESTAMP, metadata JSON
                )
                """
            )
            for task_id, metadata in rows:
                await db.execute(
                    "INSERT INTO tasks (id, title, created_by, metadata) VALUES (?, ?, 'm', ?)",
                    (task_id, task_id, json.dumps(metadata) if metadata is not None else None),
                )
            await db.commit()

    async def test_backfill_from_metadata_with_dangling_ids(self, test_config: LithosConfig):
        service = CoordinationService(test_config)
        await self._legacy_db(
            service.db_path,
            [
                ("A", None),
                ("B", {"depends_on": "A"}),  # scalar form
                ("C", {"blocked_on": ["A", "ghost"]}),  # list form + dangling
            ],
        )

        await service.initialize()

        inc_b = await service.list_task_edges("B", direction="incoming")
        assert [e["from_task_id"] for e in inc_b] == ["A"]
        assert inc_b[0]["type"] == "blocks"
        assert inc_b[0]["metadata"]["migrated_from"] == "metadata.depends_on"

        inc_c = await service.list_task_edges("C", direction="incoming")
        # "ghost" reference skipped; only "A" backfilled
        assert {e["from_task_id"] for e in inc_c} == {"A"}

    async def test_migration_is_idempotent(self, test_config: LithosConfig):
        service = CoordinationService(test_config)
        await self._legacy_db(service.db_path, [("A", None), ("B", {"depends_on": "A"})])

        await service.initialize()
        edges_first = await service.list_task_edges("B", direction="incoming")
        # re-running initialize must not error or duplicate edges
        await service.initialize()
        edges_second = await service.list_task_edges("B", direction="incoming")
        assert len(edges_first) == len(edges_second) == 1

    async def test_backfill_cycle_retained_and_surfaced(self, test_config: LithosConfig):
        service = CoordinationService(test_config)
        # A depends on B and B depends on A -> a cycle the backfill must retain
        await self._legacy_db(
            service.db_path,
            [("A", {"depends_on": "B"}), ("B", {"depends_on": "A"})],
        )
        await service.initialize()

        # both cycle members excluded from ready (mutual open blockers)
        ready_ids = _ids(await service.list_ready())
        assert "A" not in ready_ids and "B" not in ready_ids

        blocked = await service.list_blocked()
        a_row = next(t for t in blocked if t["id"] == "A")
        assert a_row["blockers"][0]["kind"] == "cycle"


# ==================== Server-level tool envelopes ====================


async def _call(server: LithosServer, tool_name: str, **kwargs: Any) -> dict[str, Any]:
    tool = await server.mcp.get_tool(tool_name)
    return await tool.fn(**kwargs)


class TestServerTaskGraphTools:
    async def test_ready_and_blocked_tools(self, server: LithosServer):
        a = await server.coordination.create_task(title="A", agent="a")
        b = await server.coordination.create_task(title="B", agent="a")
        await _call(
            server,
            "lithos_task_edge_upsert",
            from_task_id=a,
            to_task_id=b,
            type="blocks",
            agent="a",
        )

        ready = await _call(server, "lithos_task_ready")
        assert a in {t["id"] for t in ready["tasks"]}
        assert b not in {t["id"] for t in ready["tasks"]}

        blocked = await _call(server, "lithos_task_blocked")
        b_row = next(t for t in blocked["tasks"] if t["id"] == b)
        assert b_row["blockers"][0]["kind"] == "task"

    async def test_edge_upsert_cycle_error_envelope(self, server: LithosServer):
        x = await server.coordination.create_task(title="X", agent="a")
        y = await server.coordination.create_task(title="Y", agent="a")
        await _call(
            server,
            "lithos_task_edge_upsert",
            from_task_id=x,
            to_task_id=y,
            type="blocks",
            agent="a",
        )
        res = await _call(
            server,
            "lithos_task_edge_upsert",
            from_task_id=y,
            to_task_id=x,
            type="blocks",
            agent="a",
        )
        assert res["status"] == "error"
        assert res["code"] == "cycle"

    async def test_complete_returns_unblocked(self, server: LithosServer):
        a = await server.coordination.create_task(title="A", agent="a")
        b = await server.coordination.create_task(title="B", agent="a", depends_on=[a])
        res = await _call(server, "lithos_task_complete", task_id=a, agent="a")
        assert res["success"] is True
        assert res["unblocked"] == [b]

    async def test_create_bad_task_type_error_envelope(self, server: LithosServer):
        # task/epic/gate are accepted; 'subtask' was dropped entirely
        res = await _call(server, "lithos_task_create", title="T", agent="a", task_type="subtask")
        assert res["status"] == "error"
        assert res["code"] == "invalid_task_type"

    async def test_edge_list_invalid_direction_rejected(self, server: LithosServer):
        a = await server.coordination.create_task(title="A", agent="a")
        res = await _call(server, "lithos_task_edge_list", task_id=a, direction="sideways")
        assert res["status"] == "error"
        assert res["code"] == "invalid_input"

    async def test_ready_blocked_reject_non_positive_limit(self, server: LithosServer):
        await server.coordination.create_task(title="A", agent="a")
        for tool in ("lithos_task_ready", "lithos_task_blocked"):
            res = await _call(server, tool, limit=0)
            assert res["status"] == "error", tool
            assert res["code"] == "invalid_input", tool

    async def test_create_with_parent_and_children_tool(self, server: LithosServer):
        epic = await _call(server, "lithos_task_create", title="Epic", agent="a", task_type="epic")
        epic_id = epic["task_id"]
        c1 = await _call(
            server, "lithos_task_create", title="C1", agent="a", parent_task_id=epic_id
        )
        c2 = await _call(
            server, "lithos_task_create", title="C2", agent="a", parent_task_id=epic_id
        )
        children = await _call(server, "lithos_task_children", task_id=epic_id)
        assert {t["id"] for t in children["tasks"]} == {c1["task_id"], c2["task_id"]}
        # epic stays off the ready frontier
        ready = await _call(server, "lithos_task_ready")
        assert epic_id not in {t["id"] for t in ready["tasks"]}

    async def test_spawn_tool_and_invalid_relation(self, server: LithosServer):
        source = await server.coordination.create_task(title="Source", agent="a")
        ok = await _call(server, "lithos_task_spawn", source_task_id=source, title="F", agent="a")
        assert "task_id" in ok
        bad = await _call(
            server,
            "lithos_task_spawn",
            source_task_id=source,
            title="F",
            agent="a",
            relation_type="parent_child",
        )
        assert bad["status"] == "error"
        assert bad["code"] == "invalid_relation_type"

    async def test_gate_create_validation_and_blocked_kind(self, server: LithosServer):
        # invalid gate (no gate_type) -> error envelope
        bad = await _call(server, "lithos_task_create", title="G", agent="a", task_type="gate")
        assert bad["status"] == "error"
        assert bad["code"] == "invalid_input"

        # valid gate + waits_on_gate edge -> waiter shows up blocked with kind 'gate'
        gate = await _call(
            server,
            "lithos_task_create",
            title="Approval",
            agent="a",
            task_type="gate",
            metadata={"gate_type": "human"},
        )
        gate_id = gate["task_id"]
        task = await server.coordination.create_task(title="Task", agent="a")
        await _call(
            server,
            "lithos_task_edge_upsert",
            from_task_id=gate_id,
            to_task_id=task,
            type="waits_on_gate",
            agent="a",
        )
        blocked = await _call(server, "lithos_task_blocked")
        b_row = next(t for t in blocked["tasks"] if t["id"] == task)
        assert b_row["blockers"][0]["kind"] == "gate"
        # completing the gate reports the waiter as unblocked
        done = await _call(server, "lithos_task_complete", task_id=gate_id, agent="a")
        assert done["unblocked"] == [task]

    async def test_reopen_tool_envelope_finding_and_reblocked(self, server: LithosServer):
        a = await server.coordination.create_task(title="A", agent="a")
        b = await server.coordination.create_task(title="B", agent="a", depends_on=[a])
        await server.coordination.complete_task(a, "a", outcome="done")

        res = await _call(server, "lithos_task_reopen", task_id=a, agent="a")
        assert res["success"] is True
        assert res["reblocked"] == [b]  # reopening completed A re-blocks B

        # durable [Reopened] finding records the prior terminal status
        findings = await _call(server, "lithos_finding_list", task_id=a)
        assert any(
            "[Reopened]" in fnd["summary"] and "completed" in fnd["summary"]
            for fnd in findings["findings"]
        )
        # A is open again, off the resolved surface
        got = await _call(server, "lithos_task_get", task_id=a)
        assert got["task"]["status"] == "open" and got["task"]["resolved_at"] is None

    async def test_reopen_tool_error_envelopes(self, server: LithosServer):
        missing = await _call(server, "lithos_task_reopen", task_id="ghost", agent="a")
        assert missing["code"] == "task_not_found"
        open_task = await server.coordination.create_task(title="Open", agent="a")
        already = await _call(server, "lithos_task_reopen", task_id=open_task, agent="a")
        assert already["code"] == "task_not_resolved"

    async def test_update_tool_works_on_terminal_task(self, server: LithosServer):
        t = await server.coordination.create_task(title="T", agent="a")
        await server.coordination.complete_task(t, "a")
        res = await _call(server, "lithos_task_update", task_id=t, agent="a", metadata={"k": "v"})
        assert res.get("success") is True
        got = await _call(server, "lithos_task_get", task_id=t)
        assert got["task"]["metadata"]["k"] == "v" and got["task"]["status"] == "completed"
