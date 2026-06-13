"""Tests for the Phase 1 task graph: edges, ready/blocked, cycles, backfill.

Service-level tests use the ``coordination_service`` fixture; a small set of
server-level tests exercise the new MCP tool envelopes via ``server.mcp.get_tool``.
"""

import json
from typing import Any

import aiosqlite
import pytest

from lithos.config import LithosConfig
from lithos.coordination import CoordinationError, CoordinationService
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

    @pytest.mark.parametrize("bad_type", ["waits_on_gate", "duplicate_of", "relates_to", "nope"])
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

    async def test_ready_limit_with_tags_post_scan(
        self, coordination_service: CoordinationService
    ):
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
        for bad in ("epic", "gate", "subtask"):
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
        res = await _call(server, "lithos_task_create", title="T", agent="a", task_type="epic")
        assert res["status"] == "error"
        assert res["code"] == "invalid_task_type"

    async def test_edge_list_invalid_direction_rejected(self, server: LithosServer):
        a = await server.coordination.create_task(title="A", agent="a")
        res = await _call(server, "lithos_task_edge_list", task_id=a, direction="sideways")
        assert res["status"] == "error"
        assert res["code"] == "invalid_input"
