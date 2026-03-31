"""Tests for read-access audit logging (issue #130)."""

from datetime import datetime, timezone

import pytest

from lithos.coordination import AccessLogEntry, CoordinationService


class TestAuditLogSchema:
    """Tests for access_log table creation and basic operations."""

    @pytest.mark.asyncio
    async def test_log_access_read(self, coordination_service: CoordinationService):
        """log_access stores a 'read' entry."""
        await coordination_service.log_access(
            doc_id="doc-001",
            operation="read",
            agent_id="agent-a",
        )

        entries = await coordination_service.get_audit_log()
        assert any(e.doc_id == "doc-001" and e.operation == "read" for e in entries)

    @pytest.mark.asyncio
    async def test_log_access_search_result(self, coordination_service: CoordinationService):
        """log_access stores a 'search_result' entry."""
        await coordination_service.log_access(
            doc_id="doc-002",
            operation="search_result",
            agent_id="agent-b",
        )

        entries = await coordination_service.get_audit_log()
        assert any(e.doc_id == "doc-002" and e.operation == "search_result" for e in entries)

    @pytest.mark.asyncio
    async def test_log_access_defaults_to_unknown(self, coordination_service: CoordinationService):
        """log_access defaults agent_id to 'unknown'."""
        await coordination_service.log_access(doc_id="doc-003", operation="read")

        entries = await coordination_service.get_audit_log()
        entry = next((e for e in entries if e.doc_id == "doc-003"), None)
        assert entry is not None
        assert entry.agent_id == "unknown"

    @pytest.mark.asyncio
    async def test_entries_are_access_log_entry_instances(
        self, coordination_service: CoordinationService
    ):
        """get_audit_log returns AccessLogEntry dataclass instances."""
        await coordination_service.log_access(doc_id="doc-004", operation="read", agent_id="ag")

        entries = await coordination_service.get_audit_log()
        assert len(entries) >= 1
        for entry in entries:
            assert isinstance(entry, AccessLogEntry)
            assert entry.id > 0
            assert isinstance(entry.doc_id, str)
            assert entry.operation in ("read", "search_result")
            assert isinstance(entry.timestamp, datetime)

    @pytest.mark.asyncio
    async def test_entries_ordered_most_recent_first(
        self, coordination_service: CoordinationService
    ):
        """get_audit_log returns entries newest-first."""
        for i in range(3):
            await coordination_service.log_access(
                doc_id=f"doc-{i:03d}",
                operation="read",
                agent_id="agent-x",
            )

        entries = await coordination_service.get_audit_log(agent_id="agent-x")
        assert len(entries) == 3
        timestamps = [e.timestamp for e in entries if e.timestamp]
        assert timestamps == sorted(timestamps, reverse=True)


class TestAuditLogFilters:
    """Tests for get_audit_log filtering."""

    @pytest.mark.asyncio
    async def test_filter_by_agent_id(self, coordination_service: CoordinationService):
        """agent_id filter only returns matching entries."""
        await coordination_service.log_access(doc_id="d1", operation="read", agent_id="alice")
        await coordination_service.log_access(doc_id="d2", operation="read", agent_id="bob")

        entries = await coordination_service.get_audit_log(agent_id="alice")
        assert all(e.agent_id == "alice" for e in entries)
        assert any(e.doc_id == "d1" for e in entries)
        assert not any(e.doc_id == "d2" for e in entries)

    @pytest.mark.asyncio
    async def test_filter_by_after_timestamp(self, coordination_service: CoordinationService):
        """after filter excludes older entries."""
        await coordination_service.log_access(doc_id="old-doc", operation="read", agent_id="ag")

        cutoff = datetime.now(timezone.utc).isoformat()

        await coordination_service.log_access(doc_id="new-doc", operation="read", agent_id="ag")

        entries = await coordination_service.get_audit_log(after=cutoff)
        doc_ids = {e.doc_id for e in entries}
        assert "new-doc" in doc_ids
        assert "old-doc" not in doc_ids

    @pytest.mark.asyncio
    async def test_limit_respected(self, coordination_service: CoordinationService):
        """limit parameter is respected."""
        for i in range(10):
            await coordination_service.log_access(
                doc_id=f"bulk-{i}", operation="search_result", agent_id="bulk-agent"
            )

        entries = await coordination_service.get_audit_log(agent_id="bulk-agent", limit=3)
        assert len(entries) == 3

    @pytest.mark.asyncio
    async def test_limit_clamped_to_1000(self, coordination_service: CoordinationService):
        """Requesting more than 1000 entries is silently clamped."""
        # Just check it doesn't raise; we don't insert 1001 rows in a unit test
        entries = await coordination_service.get_audit_log(limit=9999)
        assert isinstance(entries, list)

    @pytest.mark.asyncio
    async def test_audit_log_filter_by_doc_id(self, coordination_service: CoordinationService):
        """doc_id filter returns only entries for that document, across all agents."""
        await coordination_service.log_access(
            doc_id="target-doc", operation="read", agent_id="agent-x"
        )
        await coordination_service.log_access(
            doc_id="target-doc", operation="search_result", agent_id="agent-y"
        )
        await coordination_service.log_access(
            doc_id="other-doc", operation="read", agent_id="agent-x"
        )

        entries = await coordination_service.get_audit_log(doc_id="target-doc")
        assert len(entries) == 2
        assert all(e.doc_id == "target-doc" for e in entries)
        # Both agents present
        agent_ids = {e.agent_id for e in entries}
        assert "agent-x" in agent_ids
        assert "agent-y" in agent_ids
        # other-doc must not appear
        assert not any(e.doc_id == "other-doc" for e in entries)

    @pytest.mark.asyncio
    async def test_audit_log_filter_by_agent_and_doc_id(
        self, coordination_service: CoordinationService
    ):
        """Filtering by both agent_id and doc_id returns only the intersection."""
        await coordination_service.log_access(
            doc_id="shared-doc", operation="read", agent_id="alice"
        )
        await coordination_service.log_access(doc_id="shared-doc", operation="read", agent_id="bob")
        await coordination_service.log_access(
            doc_id="other-doc", operation="read", agent_id="alice"
        )

        entries = await coordination_service.get_audit_log(agent_id="alice", doc_id="shared-doc")
        assert len(entries) == 1
        assert entries[0].agent_id == "alice"
        assert entries[0].doc_id == "shared-doc"


class TestRetrievalCount:
    """Tests for get_retrieval_count."""

    @pytest.mark.asyncio
    async def test_count_increments_on_read(self, coordination_service: CoordinationService):
        """Retrieval count reflects the number of 'read' operations."""
        doc_id = "counted-doc"
        assert await coordination_service.get_retrieval_count(doc_id) == 0

        await coordination_service.log_access(doc_id=doc_id, operation="read", agent_id="ag")
        assert await coordination_service.get_retrieval_count(doc_id) == 1

        await coordination_service.log_access(doc_id=doc_id, operation="read", agent_id="ag2")
        assert await coordination_service.get_retrieval_count(doc_id) == 2

    @pytest.mark.asyncio
    async def test_search_result_not_counted(self, coordination_service: CoordinationService):
        """search_result entries are NOT counted by get_retrieval_count."""
        doc_id = "search-only-doc"
        await coordination_service.log_access(
            doc_id=doc_id, operation="search_result", agent_id="ag"
        )
        assert await coordination_service.get_retrieval_count(doc_id) == 0

    @pytest.mark.asyncio
    async def test_count_zero_for_unknown_doc(self, coordination_service: CoordinationService):
        """Returns 0 for docs that have never been read."""
        count = await coordination_service.get_retrieval_count("never-read-doc")
        assert count == 0


class TestAuditLogNonFatal:
    """Tests that audit logging failures never raise."""

    @pytest.mark.asyncio
    async def test_log_access_silent_on_bad_db(self):
        """log_access swallows errors when the DB path is invalid."""
        from lithos.config import LithosConfig, StorageConfig

        config = LithosConfig(storage=StorageConfig(data_dir="/nonexistent/path/xyz"))
        service = CoordinationService(config)
        # Must not raise
        await service.log_access(doc_id="x", operation="read", agent_id="ag")


class TestLogAccessBatch:
    """Tests for log_access_batch."""

    @pytest.mark.asyncio
    async def test_batch_logs_all_doc_ids(self, coordination_service: CoordinationService):
        """log_access_batch logs every doc_id in a single call and all are queryable."""
        doc_ids = ["batch-doc-1", "batch-doc-2", "batch-doc-3"]
        await coordination_service.log_access_batch(
            doc_ids=doc_ids,
            operation="search_result",
            agent_id="batch-agent",
        )

        entries = await coordination_service.get_audit_log(agent_id="batch-agent")
        logged_doc_ids = {e.doc_id for e in entries}
        for doc_id in doc_ids:
            assert doc_id in logged_doc_ids, f"{doc_id!r} not found in audit log"

    @pytest.mark.asyncio
    async def test_batch_all_entries_have_correct_operation(
        self, coordination_service: CoordinationService
    ):
        """log_access_batch stores the correct operation for all entries."""
        doc_ids = ["op-doc-1", "op-doc-2"]
        await coordination_service.log_access_batch(
            doc_ids=doc_ids,
            operation="search_result",
            agent_id="op-agent",
        )

        entries = await coordination_service.get_audit_log(agent_id="op-agent")
        for entry in entries:
            assert entry.operation == "search_result"

    @pytest.mark.asyncio
    async def test_batch_empty_list_is_noop(self, coordination_service: CoordinationService):
        """log_access_batch with empty doc_ids list does not raise and logs nothing."""
        before = await coordination_service.get_audit_log()
        before_count = len(before)

        await coordination_service.log_access_batch(
            doc_ids=[],
            operation="read",
            agent_id="noop-agent",
        )

        after = await coordination_service.get_audit_log()
        assert len(after) == before_count

    @pytest.mark.asyncio
    async def test_batch_silent_on_bad_db(self):
        """log_access_batch swallows errors when the DB path is invalid."""
        from lithos.config import LithosConfig, StorageConfig

        config = LithosConfig(storage=StorageConfig(data_dir="/nonexistent/path/xyz"))
        service = CoordinationService(config)
        # Must not raise
        await service.log_access_batch(
            doc_ids=["d1", "d2"],
            operation="search_result",
            agent_id="ag",
        )
