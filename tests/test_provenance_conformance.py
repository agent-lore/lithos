"""Conformance test suite for provenance (US-014).

Cross-cutting tests proving provenance behavior aligns with the
unified write contract and architecture guardrails.
"""

import json
from pathlib import Path
from typing import Any

import frontmatter as fm
import pytest

from lithos.config import LithosConfig
from lithos.knowledge import KnowledgeManager
from lithos.server import LithosServer

pytestmark = pytest.mark.integration


async def _call_tool(server: LithosServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call an MCP tool and return its JSON payload."""
    result = await server.mcp._call_tool_mcp(name, arguments)
    if isinstance(result, tuple):
        payload = result[1]
        if isinstance(payload, dict):
            return payload
    content = getattr(result, "content", []) if hasattr(result, "content") else result
    if isinstance(content, list) and content:
        text = getattr(content[0], "text", None)
        if isinstance(text, str):
            return json.loads(text)
    raise AssertionError(f"Unable to decode MCP result for tool {name!r}: {result!r}")


# ---------------------------------------------------------------------------
# 1. Create semantics
# ---------------------------------------------------------------------------


class TestCreateConformance:
    """Conformance tests for create provenance semantics."""

    async def test_create_with_none_stores_empty(self, server: LithosServer):
        """create with derived_from_ids=None stores []."""
        result = await _call_tool(
            server,
            "lithos_write",
            {"title": "None Prov", "content": "Test.", "agent": "conf-agent"},
        )
        assert result["status"] == "created"
        doc_id = result["id"]
        assert server.knowledge._doc_to_sources.get(doc_id) == []

    async def test_create_with_valid_uuids_stores_normalized(self, server: LithosServer):
        """create with valid UUIDs stores normalized (sorted, lowered) list."""
        s1 = await _call_tool(
            server,
            "lithos_write",
            {"title": "Src1", "content": ".", "agent": "conf-agent"},
        )
        s2 = await _call_tool(
            server,
            "lithos_write",
            {"title": "Src2", "content": ".", "agent": "conf-agent"},
        )
        id1, id2 = s1["id"], s2["id"]

        # Pass in reverse order; expect sorted
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Derived Norm",
                "content": ".",
                "agent": "conf-agent",
                "derived_from_ids": [id2, id1],
            },
        )
        assert result["status"] == "created"
        stored = server.knowledge._doc_to_sources[result["id"]]
        assert stored == sorted([id1, id2])

    async def test_create_uppercase_normalized(self, server: LithosServer):
        """Uppercase UUIDs are normalized to lowercase, not rejected."""
        src = await _call_tool(
            server,
            "lithos_write",
            {"title": "Lower Src", "content": ".", "agent": "conf-agent"},
        )
        upper_id = src["id"].upper()
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Upper Derived",
                "content": ".",
                "agent": "conf-agent",
                "derived_from_ids": [upper_id],
            },
        )
        assert result["status"] == "created"
        stored = server.knowledge._doc_to_sources[result["id"]]
        assert stored == [src["id"]]  # lowered


# ---------------------------------------------------------------------------
# 2. Update semantics
# ---------------------------------------------------------------------------


class TestUpdateConformance:
    """Conformance tests for update provenance semantics."""

    async def _create(self, server: LithosServer, title: str, **kw: Any) -> str:
        r = await _call_tool(
            server,
            "lithos_write",
            {"title": title, "content": ".", "agent": "conf-agent", **kw},
        )
        assert r["status"] == "created"
        return r["id"]

    async def test_update_unset_preserves(self, server: LithosServer):
        """update with _UNSET (None at MCP) preserves existing provenance."""
        src_id = await self._create(server, "Pres Src")
        doc_id = await self._create(server, "Pres Derived", derived_from_ids=[src_id])
        assert server.knowledge._doc_to_sources[doc_id] == [src_id]

        # Update without derived_from_ids (None at MCP = preserve)
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Pres Derived",
                "content": "Updated.",
                "agent": "conf-agent",
                "id": doc_id,
            },
        )
        assert result["status"] == "updated"
        assert server.knowledge._doc_to_sources[doc_id] == [src_id]

    async def test_update_empty_list_clears(self, server: LithosServer):
        """update with [] clears provenance."""
        src_id = await self._create(server, "Clear Src")
        doc_id = await self._create(server, "Clear Derived", derived_from_ids=[src_id])
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Clear Derived",
                "content": "Cleared.",
                "agent": "conf-agent",
                "id": doc_id,
                "derived_from_ids": [],
            },
        )
        assert result["status"] == "updated"
        assert server.knowledge._doc_to_sources[doc_id] == []

    async def test_update_replaces(self, server: LithosServer):
        """update with new list replaces provenance."""
        s1 = await self._create(server, "Repl Src1")
        s2 = await self._create(server, "Repl Src2")
        doc_id = await self._create(server, "Repl Derived", derived_from_ids=[s1])
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Repl Derived",
                "content": "Replaced.",
                "agent": "conf-agent",
                "id": doc_id,
                "derived_from_ids": [s2],
            },
        )
        assert result["status"] == "updated"
        assert server.knowledge._doc_to_sources[doc_id] == [s2]
        # s1 no longer in reverse index for this doc
        assert doc_id not in server.knowledge._source_to_derived.get(s1, set())

    async def test_self_reference_rejected(self, server: LithosServer):
        """Self-reference on update is rejected with invalid_input."""
        doc_id = await self._create(server, "Self Ref")
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Self Ref",
                "content": "Self.",
                "agent": "conf-agent",
                "id": doc_id,
                "derived_from_ids": [doc_id],
            },
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"


# ---------------------------------------------------------------------------
# 3. Validation
# ---------------------------------------------------------------------------


class TestValidationConformance:
    """Conformance tests for provenance validation."""

    async def test_malformed_uuid_rejected(self, server: LithosServer):
        """Malformed UUIDs rejected with invalid_input."""
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Bad UUID",
                "content": ".",
                "agent": "conf-agent",
                "derived_from_ids": ["not-a-uuid"],
            },
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_input"

    async def test_non_uuid_identifiers_rejected(self, server: LithosServer):
        """Non-UUID identifiers (titles, slugs, paths) rejected."""
        for bad in ["My Document Title", "my-slug", "/path/to/file.md", "", "  "]:
            result = await _call_tool(
                server,
                "lithos_write",
                {
                    "title": f"Non UUID {bad!r}",
                    "content": ".",
                    "agent": "conf-agent",
                    "derived_from_ids": [bad],
                },
            )
            assert result["status"] == "error", f"Expected error for {bad!r}"
            assert result["code"] == "invalid_input", f"Expected invalid_input for {bad!r}"

    async def test_unresolved_produces_warnings_not_errors(self, server: LithosServer):
        """Unresolved source IDs produce warnings, not errors."""
        missing_id = "00000000-0000-0000-0000-cccccccccccc"
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Warn Not Err",
                "content": ".",
                "agent": "conf-agent",
                "derived_from_ids": [missing_id],
            },
        )
        assert result["status"] == "created"  # NOT error
        assert any(missing_id in w for w in result.get("warnings", []))


# ---------------------------------------------------------------------------
# 4. Index lifecycle
# ---------------------------------------------------------------------------


class TestIndexLifecycleConformance:
    """Conformance tests for provenance index maintenance."""

    async def test_file_watcher_auto_resolves(self, server: LithosServer):
        """File-watcher ingestion of a source doc auto-resolves unresolved provenance."""
        # Pre-create a missing source ID
        missing_id = "11111111-1111-1111-1111-111111111111"
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Waiting For Source",
                "content": ".",
                "agent": "conf-agent",
                "derived_from_ids": [missing_id],
            },
        )
        doc_id = result["id"]
        assert missing_id in server.knowledge._unresolved_provenance

        # Externally create a file with that ID (simulating git restore / file watcher)
        knowledge_path = server.knowledge.knowledge_path
        post = fm.Post("# Restored Source\n\nContent.", id=missing_id, title="Restored Source")
        (knowledge_path / "restored-source.md").write_text(fm.dumps(post))

        # Simulate file watcher event
        doc = await server.knowledge.sync_from_disk(Path("restored-source.md"))
        assert doc.id == missing_id

        # Verify auto-resolution
        assert missing_id not in server.knowledge._unresolved_provenance
        assert doc_id in server.knowledge._source_to_derived.get(missing_id, set())

    async def test_delete_source_marks_unresolved(self, server: LithosServer):
        """Deleting a source doc marks provenance as unresolved."""
        src = await _call_tool(
            server,
            "lithos_write",
            {"title": "Del Source", "content": ".", "agent": "conf-agent"},
        )
        src_id = src["id"]
        derived = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Del Derived",
                "content": ".",
                "agent": "conf-agent",
                "derived_from_ids": [src_id],
            },
        )
        derived_id = derived["id"]

        # Delete the source
        await _call_tool(server, "lithos_delete", {"id": src_id, "agent": "conf-agent"})

        # Derived doc now has unresolved provenance
        assert derived_id in server.knowledge._unresolved_provenance.get(src_id, set())
        assert src_id not in server.knowledge._source_to_derived

    async def test_cycles_no_infinite_traversal(self, server: LithosServer):
        """Provenance cycles don't cause infinite traversal."""
        a = await _call_tool(
            server,
            "lithos_write",
            {"title": "Cyc A", "content": ".", "agent": "conf-agent"},
        )
        b = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Cyc B",
                "content": ".",
                "agent": "conf-agent",
                "derived_from_ids": [a["id"]],
            },
        )
        # Create cycle: A -> B -> A
        await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Cyc A",
                "content": "Cycled.",
                "agent": "conf-agent",
                "id": a["id"],
                "derived_from_ids": [b["id"]],
            },
        )
        # This must terminate
        result = await _call_tool(
            server,
            "lithos_provenance",
            {"id": a["id"], "direction": "both", "depth": 3},
        )
        assert "sources" in result
        assert "derived" in result


# ---------------------------------------------------------------------------
# 5. Write contract parity
# ---------------------------------------------------------------------------


class TestWriteContractConformance:
    """Single-write contract parity: create/update/duplicate/warnings envelope."""

    async def test_create_returns_write_result_envelope(self, server: LithosServer):
        """Create returns status, id, path, warnings."""
        result = await _call_tool(
            server,
            "lithos_write",
            {"title": "Env Create", "content": ".", "agent": "conf-agent"},
        )
        assert result["status"] == "created"
        assert "id" in result
        assert "path" in result
        assert "warnings" in result

    async def test_update_returns_write_result_envelope(self, server: LithosServer):
        """Update returns status, id, path, warnings."""
        cr = await _call_tool(
            server,
            "lithos_write",
            {"title": "Env Update", "content": ".", "agent": "conf-agent"},
        )
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Env Update",
                "content": "Updated.",
                "agent": "conf-agent",
                "id": cr["id"],
            },
        )
        assert result["status"] == "updated"
        assert "warnings" in result

    async def test_duplicate_returns_envelope(self, server: LithosServer):
        """Duplicate returns status=duplicate with duplicate_of."""
        await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Dup Doc",
                "content": ".",
                "agent": "conf-agent",
                "source_url": "https://example.com/dup-conformance",
            },
        )
        result = await _call_tool(
            server,
            "lithos_write",
            {
                "title": "Dup Doc 2",
                "content": ".",
                "agent": "conf-agent",
                "source_url": "https://example.com/dup-conformance",
            },
        )
        assert result["status"] == "duplicate"
        assert result["duplicate_of"] is not None


# ---------------------------------------------------------------------------
# 6. Scan conformance
# ---------------------------------------------------------------------------


class TestScanConformance:
    """Conformance tests for two-pass scan and idempotent rebuild."""

    async def test_two_pass_resolves_forward_references(self, test_config: LithosConfig):
        """Two-pass scan correctly resolves forward references at startup."""
        knowledge_path = test_config.storage.knowledge_path

        id_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        id_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

        # B sorts before A alphabetically, but A references B
        post_a = fm.Post(
            "# Doc A\n\nContent.",
            id=id_a,
            title="Doc A",
            derived_from_ids=[id_b],
        )
        post_b = fm.Post("# Doc B\n\nContent.", id=id_b, title="Doc B")

        (knowledge_path / "doc-a.md").write_text(fm.dumps(post_a))
        (knowledge_path / "doc-b.md").write_text(fm.dumps(post_b))

        mgr = KnowledgeManager(test_config)

        # Forward reference resolved (A references B, B exists)
        assert mgr._doc_to_sources[id_a] == [id_b]
        assert id_a in mgr._source_to_derived.get(id_b, set())
        assert not mgr._unresolved_provenance  # no unresolved

    async def test_repeated_scan_idempotent(self, test_config: LithosConfig):
        """Repeated _scan_existing() calls produce identical index state."""
        knowledge_path = test_config.storage.knowledge_path

        id_x = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
        id_y = "yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy"

        post_x = fm.Post(
            "# X\n\nContent.",
            id=id_x,
            title="X",
            derived_from_ids=[id_y],
        )
        post_y = fm.Post("# Y\n\nContent.", id=id_y, title="Y")
        (knowledge_path / "x.md").write_text(fm.dumps(post_x))
        (knowledge_path / "y.md").write_text(fm.dumps(post_y))

        mgr = KnowledgeManager(test_config)
        snap1 = (
            dict(mgr._doc_to_sources),
            {k: set(v) for k, v in mgr._source_to_derived.items()},
            dict(mgr._unresolved_provenance),
            dict(mgr._id_to_title),
        )

        mgr._scan_existing()
        snap2 = (
            dict(mgr._doc_to_sources),
            {k: set(v) for k, v in mgr._source_to_derived.items()},
            dict(mgr._unresolved_provenance),
            dict(mgr._id_to_title),
        )

        assert snap1 == snap2

    async def test_external_title_change_updates_cache(self, server: LithosServer):
        """External title change via file watcher updates _id_to_title."""
        result = await _call_tool(
            server,
            "lithos_write",
            {"title": "Old Title", "content": ".", "agent": "conf-agent"},
        )
        doc_id = result["id"]
        assert server.knowledge._id_to_title[doc_id] == "Old Title"

        # Modify the file externally
        rel_path = server.knowledge._id_to_path[doc_id]
        full_path = server.knowledge.knowledge_path / rel_path
        post = fm.load(str(full_path))
        post.metadata["title"] = "New Title"
        post.content = "# New Title\n\nContent."
        full_path.write_text(fm.dumps(post))

        # Simulate file watcher
        await server.knowledge.sync_from_disk(rel_path)

        assert server.knowledge._id_to_title[doc_id] == "New Title"


# ---------------------------------------------------------------------------
# 9. Read path normalization consistency
# ---------------------------------------------------------------------------


class TestReadPathNormalization:
    """Conformance: MCP read/list return normalized derived_from_ids."""

    async def test_lithos_read_returns_normalized_provenance(self, server: LithosServer):
        """lithos_read returns normalized (lowercase) derived_from_ids for external files."""
        knowledge_path = server.config.storage.knowledge_path

        # Create source doc via MCP (let it generate its own id)
        src_result = await _call_tool(
            server,
            "lithos_write",
            {"title": "Source", "content": "Source content.", "agent": "test"},
        )
        source_id = src_result["id"]
        derived_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"

        # Create derived doc externally with UPPERCASE source reference
        upper_source = source_id.upper()
        post = fm.Post(
            "# Derived\n\nContent.",
            id=derived_id,
            title="Derived",
            derived_from_ids=[upper_source],
            author="external",
            created_at="2026-03-08T00:00:00+00:00",
            updated_at="2026-03-08T00:00:00+00:00",
            tags=[],
            aliases=[],
            confidence=1.0,
            contributors=[],
            source=None,
            supersedes=None,
        )
        (knowledge_path / "derived-ext.md").write_text(fm.dumps(post))
        await server.handle_file_change(knowledge_path / "derived-ext.md", deleted=False)

        # lithos_read should return normalized (lowercase) UUID
        result = await _call_tool(server, "lithos_read", {"id": derived_id})
        assert result["metadata"]["derived_from_ids"] == [source_id]

    async def test_lithos_list_returns_normalized_provenance(self, server: LithosServer):
        """lithos_list returns normalized (lowercase) derived_from_ids for external files."""
        knowledge_path = server.config.storage.knowledge_path

        # Create source doc via MCP (let it generate its own id)
        src_result = await _call_tool(
            server,
            "lithos_write",
            {"title": "ListSrc", "content": "Source.", "agent": "test"},
        )
        source_id = src_result["id"]
        derived_id = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

        # Create derived doc externally with uppercase reference
        post = fm.Post(
            "# ListDerived\n\nContent.",
            id=derived_id,
            title="ListDerived",
            derived_from_ids=[source_id.upper()],
            author="external",
            created_at="2026-03-08T00:00:00+00:00",
            updated_at="2026-03-08T00:00:00+00:00",
            tags=[],
            aliases=[],
            confidence=1.0,
            contributors=[],
            source=None,
            supersedes=None,
        )
        (knowledge_path / "list-derived.md").write_text(fm.dumps(post))
        await server.handle_file_change(knowledge_path / "list-derived.md", deleted=False)

        # lithos_list should return normalized UUIDs
        result = await _call_tool(server, "lithos_list", {})
        derived_entry = next(d for d in result["items"] if d["id"] == derived_id)
        assert derived_entry["derived_from_ids"] == [source_id]
