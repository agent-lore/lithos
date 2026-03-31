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
