"""End-to-end short-id-prefix acceptance + resolved-id echo (task 83257ced).

Exercised through FastMCP dispatch: an unambiguous >= 6-char prefix works
everywhere a full task/note id does, and every mutating response names the
full id + title it touched — so transcripts keep full ids and callers can
verify they hit the right record before the next write.
"""

from typing import Any

import pytest

from lithos.server import LithosServer
from tests.helpers import assert_error_envelope, call_tool

pytestmark = pytest.mark.integration


async def _create_task(server: LithosServer, title: str) -> str:
    created = await call_tool(server, "lithos_task_create", {"title": title, "agent": "a"})
    return created["task_id"]


class TestTaskPrefixAcceptance:
    async def test_get_by_prefix_returns_the_task(self, server: LithosServer):
        task_id = await _create_task(server, "Get Me")
        result = await call_tool(server, "lithos_task_get", {"task_id": task_id[:8]})
        assert result["task"]["id"] == task_id
        assert result["task"]["title"] == "Get Me"

    async def test_claim_by_prefix_echoes_resolved_id_and_title(self, server: LithosServer):
        task_id = await _create_task(server, "Claim Me")
        result = await call_tool(
            server,
            "lithos_task_claim",
            {"task_id": task_id[:8], "aspect": "impl", "agent": "a"},
        )
        assert result["success"] is True
        assert result["task_id"] == task_id
        assert result["title"] == "Claim Me"

    async def test_complete_by_prefix_echoes_resolved_id_and_title(self, server: LithosServer):
        task_id = await _create_task(server, "Finish Me")
        result = await call_tool(
            server, "lithos_task_complete", {"task_id": task_id[:8], "agent": "a"}
        )
        assert result["success"] is True
        assert result["task_id"] == task_id
        assert result["title"] == "Finish Me"

    async def test_update_echoes_new_title_when_changed(self, server: LithosServer):
        task_id = await _create_task(server, "Old Title")
        result = await call_tool(
            server,
            "lithos_task_update",
            {"task_id": task_id[:8], "agent": "a", "title": "New Title"},
        )
        assert result["task_id"] == task_id
        assert result["title"] == "New Title"

    async def test_create_resolves_depends_on_and_parent_prefixes(self, server: LithosServer):
        dep_id = await _create_task(server, "Dependency")
        parent_id = await _create_task(server, "Parent")
        result = await call_tool(
            server,
            "lithos_task_create",
            {
                "title": "Child",
                "agent": "a",
                "depends_on": [dep_id[:8]],
                "parent_task_id": parent_id[:8],
            },
        )
        assert result["title"] == "Child"
        assert result["depends_on"] == [dep_id]
        assert result["parent_task_id"] == parent_id

    async def test_edge_upsert_resolves_both_prefixes_and_echoes(self, server: LithosServer):
        from_id = await _create_task(server, "Blocker")
        to_id = await _create_task(server, "Blocked")
        result = await call_tool(
            server,
            "lithos_task_edge_upsert",
            {
                "from_task_id": from_id[:8],
                "to_task_id": to_id[:8],
                "type": "blocks",
                "agent": "a",
            },
        )
        assert result == {
            "success": True,
            "from_task_id": from_id,
            "from_title": "Blocker",
            "to_task_id": to_id,
            "to_title": "Blocked",
        }

    async def test_spawn_resolves_source_prefix(self, server: LithosServer):
        source_id = await _create_task(server, "Source")
        result = await call_tool(
            server,
            "lithos_task_spawn",
            {"source_task_id": source_id[:8], "title": "Spawned", "agent": "a"},
        )
        assert result["source_task_id"] == source_id
        assert result["title"] == "Spawned"

    async def test_finding_post_by_prefix_echoes_task(self, server: LithosServer):
        task_id = await _create_task(server, "Findings Host")
        result = await call_tool(
            server,
            "lithos_finding_post",
            {"task_id": task_id[:8], "agent": "a", "summary": "found it"},
        )
        assert result["task_id"] == task_id
        assert result["title"] == "Findings Host"


class TestNotePrefixAcceptance:
    async def _create_note(self, server: LithosServer, title: str) -> dict[str, Any]:
        result = await call_tool(
            server, "lithos_write", {"title": title, "content": "body", "agent": "a"}
        )
        assert result["status"] == "created"
        return result

    async def test_read_by_prefix_returns_full_id(self, server: LithosServer):
        created = await self._create_note(server, "Read Me")
        result = await call_tool(server, "lithos_read", {"id": created["id"][:8]})
        assert result["id"] == created["id"]
        assert result["title"] == "Read Me"

    async def test_write_and_note_update_echo_title(self, server: LithosServer):
        created = await self._create_note(server, "Patch Me")
        assert created["title"] == "Patch Me"
        result = await call_tool(
            server,
            "lithos_note_update",
            {"id": created["id"][:8], "agent": "a", "tags": ["tagged"]},
        )
        assert result["status"] == "updated"
        assert result["id"] == created["id"]
        assert result["title"] == "Patch Me"

    async def test_delete_by_prefix_echoes_id_title_path(self, server: LithosServer):
        created = await self._create_note(server, "Delete Me")
        result = await call_tool(server, "lithos_delete", {"id": created["id"][:8], "agent": "a"})
        assert result["success"] is True
        assert result["id"] == created["id"]
        assert result["title"] == "Delete Me"
        assert result["path"] == created["path"]

    async def test_write_with_unknown_full_id_is_note_not_found(self, server: LithosServer):
        """Regression pin: this used to escape as an uncaught FileNotFoundError
        (a protocol-level ToolError), not an envelope."""
        result = await call_tool(
            server,
            "lithos_write",
            {
                "id": "00000000-0000-4000-8000-000000000000",
                "title": "t",
                "content": "c",
                "agent": "a",
            },
        )
        assert_error_envelope(result, code="note_not_found")

    async def test_related_and_node_stats_accept_prefixes(self, server: LithosServer):
        created = await self._create_note(server, "Neighbourly")
        related = await call_tool(server, "lithos_related", {"id": created["id"][:8]})
        assert related["id"] == created["id"]
        stats = await call_tool(server, "lithos_node_stats", {"node_id": created["id"][:8]})
        assert "status" not in stats or stats.get("status") != "error"


class TestReferenceFieldResolution:
    """PR #412 review round: id-bearing fields that persist or filter must
    resolve too — a display prefix must never become silent wrong state."""

    async def test_write_source_task_persists_full_id(self, server: LithosServer):
        task_id = await _create_task(server, "Provenance Task")
        created = await call_tool(
            server,
            "lithos_write",
            {
                "title": "Sourced Note",
                "content": "body",
                "agent": "a",
                "source_task": task_id[:8],
            },
        )
        assert created["status"] == "created"
        read = await call_tool(server, "lithos_read", {"id": created["id"]})
        # source_task persists as the frontmatter field `source`
        assert read["metadata"]["source"] == task_id

    async def test_write_free_form_source_task_passes_through(self, server: LithosServer):
        """source_task is a lenient reference: correlation keys and
        cross-environment ids that match no task are stored as given."""
        result = await call_tool(
            server,
            "lithos_write",
            {"title": "Keyed Note", "content": "c", "agent": "a", "source_task": "task-wm-style"},
        )
        assert result["status"] == "created"
        read = await call_tool(server, "lithos_read", {"id": result["id"]})
        assert read["metadata"]["source"] == "task-wm-style"

    async def test_derived_from_ids_prefix_resolves_forward_ref_passes(self, server: LithosServer):
        source = await call_tool(
            server, "lithos_write", {"title": "Source Note", "content": "s", "agent": "a"}
        )
        ghost = "00000000-0000-4000-8000-00000000dead"
        derived = await call_tool(
            server,
            "lithos_write",
            {
                "title": "Derived Note",
                "content": "d",
                "agent": "a",
                "derived_from_ids": [source["id"][:8], ghost],
            },
        )
        assert derived["status"] == "created"
        read = await call_tool(server, "lithos_read", {"id": derived["id"]})
        # codec normalization does not preserve list order
        assert set(read["metadata"]["derived_from_ids"]) == {source["id"], ghost}

    async def test_finding_knowledge_id_prefix_resolves(self, server: LithosServer):
        task_id = await _create_task(server, "Linked Findings")
        note = await call_tool(
            server, "lithos_write", {"title": "Evidence Note", "content": "e", "agent": "a"}
        )
        await call_tool(
            server,
            "lithos_finding_post",
            {
                "task_id": task_id,
                "agent": "a",
                "summary": "linked",
                "knowledge_id": note["id"][:8],
            },
        )
        findings = await call_tool(server, "lithos_finding_list", {"task_id": task_id})
        assert findings["findings"][0]["knowledge_id"] == note["id"]

    async def test_finding_unknown_full_knowledge_id_stored_verbatim(self, server: LithosServer):
        task_id = await _create_task(server, "Orphan Link")
        ghost = "00000000-0000-4000-8000-00000000beef"
        await call_tool(
            server,
            "lithos_finding_post",
            {"task_id": task_id, "agent": "a", "summary": "orphan", "knowledge_id": ghost},
        )
        findings = await call_tool(server, "lithos_finding_list", {"task_id": task_id})
        assert findings["findings"][0]["knowledge_id"] == ghost

    async def test_edge_upsert_prefix_endpoints_resolve_and_filter(self, server: LithosServer):
        a = await call_tool(
            server, "lithos_write", {"title": "Edge Src", "content": "x", "agent": "a"}
        )
        b = await call_tool(
            server, "lithos_write", {"title": "Edge Dst", "content": "y", "agent": "a"}
        )
        upsert = await call_tool(
            server,
            "lithos_edge_upsert",
            {
                "from_id": a["id"][:8],
                "to_id": b["id"][:8],
                "type": "related_to",
                "weight": 0.5,
                "namespace": "test",
            },
        )
        assert upsert["status"] == "ok"
        by_full = await call_tool(server, "lithos_edge_list", {"from_id": a["id"]})
        assert [e for e in by_full["results"] if e["to_id"] == b["id"]]
        by_prefix = await call_tool(server, "lithos_edge_list", {"from_id": a["id"][:8]})
        assert [e for e in by_prefix["results"] if e["to_id"] == b["id"]]

    async def test_edge_upsert_free_form_node_ids_still_work(self, server: LithosServer):
        upsert = await call_tool(
            server,
            "lithos_edge_upsert",
            {
                "from_id": "concept:alpha",
                "to_id": "concept:beta",
                "type": "related_to",
                "weight": 0.1,
                "namespace": "test",
            },
        )
        assert upsert["status"] == "ok"
        listed = await call_tool(server, "lithos_edge_list", {"from_id": "concept:alpha"})
        assert [e for e in listed["results"] if e["to_id"] == "concept:beta"]

    async def test_retrieve_free_form_task_id_passes_through(self, server: LithosServer):
        """retrieve.task_id doubles as a correlation key — a value matching
        no task must keep working, not error."""
        result = await call_tool(
            server, "lithos_retrieve", {"query": "anything", "task_id": "task-wm-style"}
        )
        assert "results" in result

    async def test_retrieve_task_prefix_matches_full_id_scoping(self, server: LithosServer):
        """The reviewer's probe: a task-scoped note visible with the full task
        id must be equally visible when retrieval uses its prefix."""
        task_id = await _create_task(server, "Scoped Retrieval")
        note = await call_tool(
            server,
            "lithos_write",
            {
                "title": "Zanzibar Deployment Runbook",
                "content": "The zanzibar cutover requires the amber checklist.",
                "agent": "scoped-agent",
                "access_scope": "task",
                "source_task": task_id,
            },
        )
        assert note["status"] == "created"

        async def _ids(tid: str) -> set[str]:
            result = await call_tool(
                server,
                "lithos_retrieve",
                {
                    "query": "zanzibar cutover amber checklist",
                    "agent_id": "scoped-agent",
                    "task_id": tid,
                },
            )
            assert "results" in result, result
            return {r["id"] for r in result["results"]}

        assert note["id"] in await _ids(task_id)
        assert note["id"] in await _ids(task_id[:8])
