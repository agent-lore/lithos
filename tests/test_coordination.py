"""Tests for coordination module - tasks, claims, agents, findings."""

import asyncio
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from lithos.config import LithosConfig, StorageConfig
from lithos.coordination import CoordinationService


class TestAgentRegistry:
    """Tests for agent registration and tracking."""

    @pytest.mark.asyncio
    async def test_register_new_agent(self, coordination_service: CoordinationService):
        """Register a new agent with full details."""
        success, created = await coordination_service.register_agent(
            agent_id="agent-001",
            name="Test Agent",
            agent_type="test",
            metadata={"version": "1.0"},
        )

        assert success
        assert created  # New agent

    @pytest.mark.asyncio
    async def test_register_existing_agent_updates(self, coordination_service: CoordinationService):
        """Re-registering updates existing agent."""
        await coordination_service.register_agent(
            agent_id="agent-002",
            name="Original Name",
        )

        success, created = await coordination_service.register_agent(
            agent_id="agent-002",
            name="Updated Name",
        )

        assert success
        assert not created  # Existing agent

        agent = await coordination_service.get_agent("agent-002")
        assert agent.name == "Updated Name"

    @pytest.mark.asyncio
    async def test_auto_registration_on_activity(self, coordination_service: CoordinationService):
        """Agents are auto-registered on first activity."""
        # Create task with unknown agent
        await coordination_service.create_task(
            title="Test Task",
            agent="auto-registered-agent",
        )

        # Agent should now exist
        agent = await coordination_service.get_agent("auto-registered-agent")
        assert agent is not None
        assert agent.id == "auto-registered-agent"

    @pytest.mark.asyncio
    async def test_get_nonexistent_agent(self, coordination_service: CoordinationService):
        """Getting nonexistent agent returns None."""
        agent = await coordination_service.get_agent("nonexistent")
        assert agent is None

    @pytest.mark.asyncio
    async def test_list_agents(self, coordination_service: CoordinationService):
        """List all registered agents."""
        await coordination_service.register_agent("agent-a", agent_type="type-1")
        await coordination_service.register_agent("agent-b", agent_type="type-2")
        await coordination_service.register_agent("agent-c", agent_type="type-1")

        all_agents = await coordination_service.list_agents()
        assert len(all_agents) >= 3

    @pytest.mark.asyncio
    async def test_list_agents_filter_by_type(self, coordination_service: CoordinationService):
        """Filter agents by type."""
        await coordination_service.register_agent("filter-agent-1", agent_type="special")
        await coordination_service.register_agent("filter-agent-2", agent_type="normal")
        await coordination_service.register_agent("filter-agent-3", agent_type="special")

        special_agents = await coordination_service.list_agents(agent_type="special")

        assert all(a.type == "special" for a in special_agents)
        assert len(special_agents) >= 2

    @pytest.mark.asyncio
    async def test_last_seen_updated(self, coordination_service: CoordinationService):
        """Agent last_seen_at is updated on activity."""
        await coordination_service.register_agent("activity-agent")

        agent_before = await coordination_service.get_agent("activity-agent")
        first_seen = agent_before.last_seen_at

        # Small delay
        await asyncio.sleep(0.1)

        # Activity updates last_seen
        await coordination_service.ensure_agent_known("activity-agent")

        agent_after = await coordination_service.get_agent("activity-agent")
        assert agent_after.last_seen_at >= first_seen


class TestTaskLifecycle:
    """Tests for task creation and lifecycle."""

    @pytest.mark.asyncio
    async def test_create_task(self, coordination_service: CoordinationService):
        """Create a new task."""
        task_id = await coordination_service.create_task(
            title="Research API Design",
            agent="researcher",
            description="Investigate best practices for REST API design.",
            tags=["research", "api"],
        )

        assert task_id is not None
        assert len(task_id) == 36  # UUID format

    @pytest.mark.asyncio
    async def test_get_task(self, coordination_service: CoordinationService):
        """Retrieve task by ID."""
        task_id = await coordination_service.create_task(
            title="Test Task",
            agent="agent",
            description="Description here.",
        )

        task = await coordination_service.get_task(task_id)

        assert task is not None
        assert task.id == task_id
        assert task.title == "Test Task"
        assert task.status == "open"

    @pytest.mark.asyncio
    async def test_complete_task(self, coordination_service: CoordinationService):
        """Complete a task."""
        task_id = await coordination_service.create_task(
            title="Completable Task",
            agent="agent",
        )

        success = await coordination_service.complete_task(task_id, "agent")

        assert success
        task = await coordination_service.get_task(task_id)
        assert task.status == "completed"

    @pytest.mark.asyncio
    async def test_complete_task_persists_outcome(self, coordination_service: CoordinationService):
        """complete_task(outcome=...) persists the free-text summary and resolved_at."""
        task_id = await coordination_service.create_task(
            title="Task with Outcome",
            agent="agent",
        )

        outcome = "Resolved: salience weights recalibrated and smoke tested."
        success = await coordination_service.complete_task(task_id, "agent", outcome=outcome)

        assert success
        task = await coordination_service.get_task(task_id)
        assert task is not None
        assert task.status == "completed"
        assert task.outcome == outcome
        assert task.resolved_at is not None

    @pytest.mark.asyncio
    async def test_complete_task_outcome_defaults_none(
        self, coordination_service: CoordinationService
    ):
        """Omitting outcome is backward-compatible — task.outcome is None."""
        task_id = await coordination_service.create_task(
            title="Task without Outcome",
            agent="agent",
        )

        success = await coordination_service.complete_task(task_id, "agent")

        assert success
        task = await coordination_service.get_task(task_id)
        assert task is not None
        assert task.status == "completed"
        assert task.outcome is None
        assert task.resolved_at is not None

    @pytest.mark.asyncio
    async def test_complete_releases_claims(self, coordination_service: CoordinationService):
        """Completing task releases all claims."""
        task_id = await coordination_service.create_task(
            title="Task with Claims",
            agent="agent",
        )

        # Create claims
        await coordination_service.claim_task(task_id, "research", "agent-1")
        await coordination_service.claim_task(task_id, "implementation", "agent-2")

        # Complete task
        await coordination_service.complete_task(task_id, "agent")

        # Claims should be released
        statuses = await coordination_service.get_task_status(task_id)
        assert len(statuses[0].claims) == 0

    @pytest.mark.asyncio
    async def test_complete_already_completed_fails(
        self, coordination_service: CoordinationService
    ):
        """Cannot complete already completed task."""
        task_id = await coordination_service.create_task(
            title="Already Done",
            agent="agent",
        )

        await coordination_service.complete_task(task_id, "agent")
        success = await coordination_service.complete_task(task_id, "agent")

        assert not success

    @pytest.mark.asyncio
    async def test_cancel_task(self, coordination_service: CoordinationService):
        """Cancel a task."""
        task_id = await coordination_service.create_task(
            title="Cancellable Task",
            agent="agent",
        )

        success = await coordination_service.cancel_task(task_id, "agent")

        assert success
        task = await coordination_service.get_task(task_id)
        assert task.status == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_releases_claims(self, coordination_service: CoordinationService):
        """Cancelling task releases all claims."""
        task_id = await coordination_service.create_task(
            title="Task with Claims",
            agent="agent",
        )
        await coordination_service.claim_task(task_id, "research", "agent-1")
        await coordination_service.claim_task(task_id, "implementation", "agent-2")

        await coordination_service.cancel_task(task_id, "agent")

        statuses = await coordination_service.get_task_status(task_id)
        assert len(statuses[0].claims) == 0

    @pytest.mark.asyncio
    async def test_cancel_already_cancelled_fails(self, coordination_service: CoordinationService):
        """Cannot cancel already cancelled task."""
        task_id = await coordination_service.create_task(
            title="Already Cancelled",
            agent="agent",
        )

        await coordination_service.cancel_task(task_id, "agent")
        success = await coordination_service.cancel_task(task_id, "agent")

        assert not success

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_task_fails(self, coordination_service: CoordinationService):
        """Cannot cancel a task that does not exist."""
        success = await coordination_service.cancel_task("nonexistent-id", "agent")
        assert not success

    @pytest.mark.asyncio
    async def test_cancel_completed_task_fails(self, coordination_service: CoordinationService):
        """Cannot cancel an already completed task."""
        task_id = await coordination_service.create_task(
            title="Done Task",
            agent="agent",
        )
        await coordination_service.complete_task(task_id, "agent")

        success = await coordination_service.cancel_task(task_id, "agent")

        assert not success

    @pytest.mark.asyncio
    async def test_cancel_task_writes_resolved_at(self, coordination_service: CoordinationService):
        """cancel_task dual-writes resolved_at alongside the status flip (#286)."""
        before = datetime.now(timezone.utc)
        task_id = await coordination_service.create_task(
            title="Cancellable Task",
            agent="agent",
        )

        success = await coordination_service.cancel_task(task_id, "agent")
        assert success

        task = await coordination_service.get_task(task_id)
        assert task is not None
        assert task.status == "cancelled"
        assert task.resolved_at is not None
        # The dual-write timestamp must be in [before, now]. We allow ~5s of
        # slack to absorb clock skew under heavy CI load.
        after = datetime.now(timezone.utc)
        assert before - timedelta(seconds=5) <= task.resolved_at <= after + timedelta(seconds=5)

    @pytest.mark.asyncio
    async def test_get_task_status(self, coordination_service: CoordinationService):
        """Get task status with claims."""
        task_id = await coordination_service.create_task(
            title="Status Test",
            agent="agent",
        )
        await coordination_service.claim_task(task_id, "aspect-1", "agent-1")

        statuses = await coordination_service.get_task_status(task_id)

        assert len(statuses) == 1
        assert statuses[0].id == task_id
        assert len(statuses[0].claims) == 1
        assert statuses[0].claims[0].aspect == "aspect-1"

    @pytest.mark.asyncio
    async def test_get_all_active_tasks(self, coordination_service: CoordinationService):
        """Get all active (open) tasks."""
        task1 = await coordination_service.create_task(title="Active 1", agent="agent")
        task2 = await coordination_service.create_task(title="Active 2", agent="agent")
        task3 = await coordination_service.create_task(title="Completed", agent="agent")
        await coordination_service.complete_task(task3, "agent")

        statuses = await coordination_service.get_task_status()

        task_ids = [s.id for s in statuses]
        assert task1 in task_ids
        assert task2 in task_ids
        assert task3 not in task_ids  # Completed, not active

    @pytest.mark.asyncio
    async def test_get_all_tasks_when_include_all_true(
        self, coordination_service: CoordinationService
    ):
        """include_all returns open and completed tasks."""
        open_task = await coordination_service.create_task(title="Open Task", agent="agent")
        done_task = await coordination_service.create_task(title="Done Task", agent="agent")
        await coordination_service.complete_task(done_task, "agent")

        statuses = await coordination_service.get_task_status(include_all=True)
        task_ids = {s.id for s in statuses}

        assert open_task in task_ids
        assert done_task in task_ids


class TestClaimManagement:
    """Tests for task claim operations."""

    @pytest.mark.asyncio
    async def test_claim_task_aspect(self, coordination_service: CoordinationService):
        """Claim an aspect of a task."""
        task_id = await coordination_service.create_task(
            title="Claimable Task",
            agent="creator",
        )

        success, expires_at = await coordination_service.claim_task(
            task_id=task_id,
            aspect="research",
            agent="researcher",
            ttl_minutes=30,
        )

        assert success
        assert expires_at is not None
        assert expires_at > datetime.now(timezone.utc)

    @pytest.mark.asyncio
    async def test_claim_conflict_different_agent(self, coordination_service: CoordinationService):
        """Different agent cannot claim already claimed aspect."""
        task_id = await coordination_service.create_task(
            title="Contested Task",
            agent="creator",
        )

        # First agent claims
        success1, _ = await coordination_service.claim_task(
            task_id=task_id,
            aspect="implementation",
            agent="agent-1",
        )

        # Second agent tries to claim same aspect
        success2, _ = await coordination_service.claim_task(
            task_id=task_id,
            aspect="implementation",
            agent="agent-2",
        )

        assert success1
        assert not success2  # Conflict!

    @pytest.mark.asyncio
    async def test_same_agent_can_reclaim(self, coordination_service: CoordinationService):
        """Same agent can extend their own claim."""
        task_id = await coordination_service.create_task(
            title="Reclaim Task",
            agent="creator",
        )

        success1, expires1 = await coordination_service.claim_task(
            task_id=task_id,
            aspect="work",
            agent="worker",
            ttl_minutes=30,
        )

        success2, expires2 = await coordination_service.claim_task(
            task_id=task_id,
            aspect="work",
            agent="worker",
            ttl_minutes=60,
        )

        assert success1
        assert success2
        assert expires2 > expires1  # Extended

    @pytest.mark.asyncio
    async def test_multiple_aspects_same_task(self, coordination_service: CoordinationService):
        """Different aspects can be claimed by different agents."""
        task_id = await coordination_service.create_task(
            title="Multi-aspect Task",
            agent="creator",
        )

        success1, _ = await coordination_service.claim_task(
            task_id=task_id,
            aspect="research",
            agent="researcher",
        )
        success2, _ = await coordination_service.claim_task(
            task_id=task_id,
            aspect="implementation",
            agent="developer",
        )
        success3, _ = await coordination_service.claim_task(
            task_id=task_id,
            aspect="testing",
            agent="tester",
        )

        assert success1 and success2 and success3

    @pytest.mark.asyncio
    async def test_renew_claim(self, coordination_service: CoordinationService):
        """Renew an existing claim."""
        task_id = await coordination_service.create_task(
            title="Renewable Task",
            agent="creator",
        )

        await coordination_service.claim_task(
            task_id=task_id,
            aspect="work",
            agent="worker",
            ttl_minutes=30,
        )

        success, new_expires = await coordination_service.renew_claim(
            task_id=task_id,
            aspect="work",
            agent="worker",
            ttl_minutes=60,
        )

        assert success
        assert new_expires > datetime.now(timezone.utc) + timedelta(minutes=55)

    @pytest.mark.asyncio
    async def test_renew_others_claim_fails(self, coordination_service: CoordinationService):
        """Cannot renew another agent's claim."""
        task_id = await coordination_service.create_task(
            title="Others Claim",
            agent="creator",
        )

        await coordination_service.claim_task(
            task_id=task_id,
            aspect="work",
            agent="original-owner",
        )

        success, _ = await coordination_service.renew_claim(
            task_id=task_id,
            aspect="work",
            agent="different-agent",
        )

        assert not success

    @pytest.mark.asyncio
    async def test_release_claim(self, coordination_service: CoordinationService):
        """Release a claim voluntarily."""
        task_id = await coordination_service.create_task(
            title="Releasable Task",
            agent="creator",
        )

        await coordination_service.claim_task(
            task_id=task_id,
            aspect="work",
            agent="worker",
        )

        success = await coordination_service.release_claim(
            task_id=task_id,
            aspect="work",
            agent="worker",
        )

        assert success

        # Another agent can now claim
        success2, _ = await coordination_service.claim_task(
            task_id=task_id,
            aspect="work",
            agent="new-worker",
        )
        assert success2

    @pytest.mark.asyncio
    async def test_release_others_claim_fails(self, coordination_service: CoordinationService):
        """Cannot release another agent's claim."""
        task_id = await coordination_service.create_task(
            title="Protected Claim",
            agent="creator",
        )

        await coordination_service.claim_task(
            task_id=task_id,
            aspect="work",
            agent="owner",
        )

        success = await coordination_service.release_claim(
            task_id=task_id,
            aspect="work",
            agent="attacker",
        )

        assert not success

    @pytest.mark.asyncio
    async def test_claim_on_completed_task_fails(self, coordination_service: CoordinationService):
        """Cannot claim aspects of completed tasks."""
        task_id = await coordination_service.create_task(
            title="Done Task",
            agent="creator",
        )
        await coordination_service.complete_task(task_id, "creator")

        success, _ = await coordination_service.claim_task(
            task_id=task_id,
            aspect="work",
            agent="late-agent",
        )

        assert not success

    @pytest.mark.asyncio
    async def test_ttl_clamped_to_max(self, coordination_service: CoordinationService):
        """TTL is clamped to maximum allowed value."""
        task_id = await coordination_service.create_task(
            title="Long Claim Task",
            agent="creator",
        )

        # Request very long TTL
        success, expires_at = await coordination_service.claim_task(
            task_id=task_id,
            aspect="work",
            agent="worker",
            ttl_minutes=99999,  # Way too long
        )

        assert success
        # Should be clamped to max (480 minutes = 8 hours by default)
        max_allowed = datetime.now(timezone.utc) + timedelta(minutes=481)
        assert expires_at < max_allowed

    @pytest.mark.asyncio
    async def test_negative_ttl_clamped_to_min(self, coordination_service: CoordinationService):
        """Negative TTL is clamped to minimum positive duration."""
        task_id = await coordination_service.create_task(
            title="Negative TTL Task",
            agent="creator",
        )

        success, expires_at = await coordination_service.claim_task(
            task_id=task_id,
            aspect="work",
            agent="worker",
            ttl_minutes=-10,
        )

        assert success
        assert expires_at is not None
        lower_bound = datetime.now(timezone.utc) + timedelta(seconds=30)
        upper_bound = datetime.now(timezone.utc) + timedelta(minutes=2)
        assert lower_bound < expires_at < upper_bound

    @pytest.mark.asyncio
    async def test_concurrent_claim_only_one_succeeds(
        self, coordination_service: CoordinationService
    ):
        """When two coroutines race to claim the same task aspect, exactly one wins.

        This exercises the TOCTOU fix: the atomic INSERT…ON CONFLICT DO UPDATE
        WHERE clause ensures that only one claimant wins even when both read
        'no active claim' at the same moment.
        """
        task_id = await coordination_service.create_task(
            title="Contested Task",
            agent="creator",
        )

        # Fire both claims concurrently — asyncio.gather runs them interleaved
        # on the same event loop, maximising the chance of a race.
        results = await asyncio.gather(
            coordination_service.claim_task(task_id, "work", "agent-alpha"),
            coordination_service.claim_task(task_id, "work", "agent-beta"),
            return_exceptions=False,
        )

        successes = [r for r in results if r[0] is True]
        failures = [r for r in results if r[0] is False]

        assert len(successes) == 1, (
            f"Expected exactly one winner, got successes={successes} failures={failures}"
        )
        assert len(failures) == 1, (
            f"Expected exactly one loser, got successes={successes} failures={failures}"
        )
        # The losing claim should return (False, None) — no expiry
        assert failures[0][1] is None


class TestFindings:
    """Tests for task findings."""

    @pytest.mark.asyncio
    async def test_post_finding(self, coordination_service: CoordinationService):
        """Post a finding to a task."""
        task_id = await coordination_service.create_task(
            title="Research Task",
            agent="researcher",
        )

        finding_id = await coordination_service.post_finding(
            task_id=task_id,
            agent="researcher",
            summary="Found relevant documentation in the API specs.",
            knowledge_id="doc-123",
        )

        assert finding_id is not None
        assert len(finding_id) == 36

    @pytest.mark.asyncio
    async def test_list_findings(self, coordination_service: CoordinationService):
        """List all findings for a task."""
        task_id = await coordination_service.create_task(
            title="Multi-finding Task",
            agent="agent",
        )

        await coordination_service.post_finding(
            task_id=task_id,
            agent="agent-1",
            summary="First finding",
        )
        await coordination_service.post_finding(
            task_id=task_id,
            agent="agent-2",
            summary="Second finding",
        )

        findings = await coordination_service.list_findings(task_id)

        assert len(findings) == 2
        summaries = [f.summary for f in findings]
        assert "First finding" in summaries
        assert "Second finding" in summaries

    @pytest.mark.asyncio
    async def test_findings_ordered_by_time(self, coordination_service: CoordinationService):
        """Findings are returned in chronological order."""
        task_id = await coordination_service.create_task(
            title="Ordered Findings",
            agent="agent",
        )

        await coordination_service.post_finding(
            task_id=task_id,
            agent="agent",
            summary="First",
        )
        await asyncio.sleep(0.05)
        await coordination_service.post_finding(
            task_id=task_id,
            agent="agent",
            summary="Second",
        )
        await asyncio.sleep(0.05)
        await coordination_service.post_finding(
            task_id=task_id,
            agent="agent",
            summary="Third",
        )

        findings = await coordination_service.list_findings(task_id)

        assert findings[0].summary == "First"
        assert findings[1].summary == "Second"
        assert findings[2].summary == "Third"

    @pytest.mark.asyncio
    async def test_findings_filter_by_since(self, coordination_service: CoordinationService):
        """Filter findings by timestamp."""
        task_id = await coordination_service.create_task(
            title="Filtered Findings",
            agent="agent",
        )

        await coordination_service.post_finding(
            task_id=task_id,
            agent="agent",
            summary="Old finding",
        )

        cutoff = datetime.now(timezone.utc)
        await asyncio.sleep(0.05)

        await coordination_service.post_finding(
            task_id=task_id,
            agent="agent",
            summary="New finding",
        )

        findings = await coordination_service.list_findings(task_id, since=cutoff)

        assert len(findings) == 1
        assert findings[0].summary == "New finding"


class TestListTasksMetadataMatch:
    """Tests for list_tasks metadata_match filtering via SQL pushdown (#306)."""

    @pytest.mark.asyncio
    async def test_scalar_match(self, coordination_service: CoordinationService):
        t1 = await coordination_service.create_task(
            title="A", agent="agent", metadata={"priority": "high"}
        )
        await coordination_service.create_task(
            title="B", agent="agent", metadata={"priority": "low"}
        )
        tasks = await coordination_service.list_tasks(metadata_match={"priority": "high"})
        assert [t["id"] for t in tasks] == [t1]

    @pytest.mark.asyncio
    async def test_no_match(self, coordination_service: CoordinationService):
        await coordination_service.create_task(title="A", agent="agent", metadata={"k": "v"})
        assert await coordination_service.list_tasks(metadata_match={"k": "other"}) == []
        assert await coordination_service.list_tasks(metadata_match={"missing": "x"}) == []

    @pytest.mark.asyncio
    async def test_multi_key_and(self, coordination_service: CoordinationService):
        t1 = await coordination_service.create_task(
            title="A", agent="agent", metadata={"repo": "x", "watch": True}
        )
        await coordination_service.create_task(
            title="B", agent="agent", metadata={"repo": "x", "watch": False}
        )
        tasks = await coordination_service.list_tasks(metadata_match={"repo": "x", "watch": True})
        assert [t["id"] for t in tasks] == [t1]

    @pytest.mark.asyncio
    async def test_type_fidelity(self, coordination_service: CoordinationService):
        await coordination_service.create_task(title="A", agent="agent", metadata={"n": 3})
        assert len(await coordination_service.list_tasks(metadata_match={"n": 3})) == 1
        assert await coordination_service.list_tasks(metadata_match={"n": "3"}) == []

    @pytest.mark.asyncio
    async def test_list_contains(self, coordination_service: CoordinationService):
        t1 = await coordination_service.create_task(
            title="A", agent="agent", metadata={"github_repos": ["org/a", "org/b"]}
        )
        await coordination_service.create_task(
            title="B", agent="agent", metadata={"github_repos": ["org/c"]}
        )
        hit = await coordination_service.list_tasks(metadata_match={"github_repos": "org/a"})
        assert [t["id"] for t in hit] == [t1]
        assert await coordination_service.list_tasks(metadata_match={"github_repos": "org/z"}) == []

    @pytest.mark.asyncio
    async def test_bool_not_matched_by_int(self, coordination_service: CoordinationService):
        """Type-sensitive: a stored JSON bool must not match an int query (and
        vice versa), even though SQLite stores booleans as 1/0."""
        bool_task = await coordination_service.create_task(
            title="bool", agent="agent", metadata={"watch": True}
        )
        int_task = await coordination_service.create_task(
            title="int", agent="agent", metadata={"watch": 1}
        )
        by_true = await coordination_service.list_tasks(metadata_match={"watch": True})
        by_one = await coordination_service.list_tasks(metadata_match={"watch": 1})
        assert [t["id"] for t in by_true] == [bool_task]
        assert [t["id"] for t in by_one] == [int_task]

    @pytest.mark.asyncio
    async def test_object_value_not_matched_by_contains(
        self, coordination_service: CoordinationService
    ):
        """A stored JSON object must not be treated as a 'contains' collection —
        only arrays are iterated."""
        await coordination_service.create_task(
            title="obj", agent="agent", metadata={"repos": {"nested": "org/a"}}
        )
        assert await coordination_service.list_tasks(metadata_match={"repos": "org/a"}) == []

    @pytest.mark.asyncio
    async def test_composes_with_status_filter(self, coordination_service: CoordinationService):
        t1 = await coordination_service.create_task(
            title="A", agent="agent", metadata={"team": "x"}
        )
        t2 = await coordination_service.create_task(
            title="B", agent="agent", metadata={"team": "x"}
        )
        await coordination_service.complete_task(t2, agent="agent", outcome="done")
        tasks = await coordination_service.list_tasks(status="open", metadata_match={"team": "x"})
        assert [t["id"] for t in tasks] == [t1]

    @pytest.mark.asyncio
    async def test_injection_style_key_is_inert(self, coordination_service: CoordinationService):
        await coordination_service.create_task(title="A", agent="agent", metadata={"k": "v"})
        # A key crafted to look like SQL/JSON-path injection is bound as a
        # parameter, so it simply addresses a (nonexistent) key → no match,
        # and the table is intact afterwards.
        evil = '") = 1 OR "1"=("1'
        assert await coordination_service.list_tasks(metadata_match={evil: "v"}) == []
        assert len(await coordination_service.list_tasks()) == 1


class TestListTasks:
    """Tests for list_tasks filtering."""

    @pytest.mark.asyncio
    async def test_list_all_tasks(self, coordination_service: CoordinationService):
        """list_tasks with no filters returns all tasks."""
        t1 = await coordination_service.create_task(title="Task A", agent="agent-x")
        t2 = await coordination_service.create_task(title="Task B", agent="agent-y")

        tasks = await coordination_service.list_tasks()
        ids = [t["id"] for t in tasks]
        assert t1 in ids
        assert t2 in ids

    @pytest.mark.asyncio
    async def test_list_tasks_filter_by_agent(self, coordination_service: CoordinationService):
        """Filter tasks by creating agent."""
        t1 = await coordination_service.create_task(title="By Alpha", agent="alpha")
        await coordination_service.create_task(title="By Beta", agent="beta")

        tasks = await coordination_service.list_tasks(agent="alpha")
        ids = [t["id"] for t in tasks]
        assert t1 in ids
        assert all(t["created_by"] == "alpha" for t in tasks)

    @pytest.mark.asyncio
    async def test_list_tasks_filter_by_status(self, coordination_service: CoordinationService):
        """Filter tasks by status."""
        open_id = await coordination_service.create_task(title="Open Task", agent="agent")
        done_id = await coordination_service.create_task(title="Done Task", agent="agent")
        await coordination_service.complete_task(done_id, "agent")

        open_tasks = await coordination_service.list_tasks(status="open")
        open_ids = [t["id"] for t in open_tasks]
        assert open_id in open_ids
        assert done_id not in open_ids

        done_tasks = await coordination_service.list_tasks(status="completed")
        done_ids = [t["id"] for t in done_tasks]
        assert done_id in done_ids
        assert open_id not in done_ids

    @pytest.mark.asyncio
    async def test_list_tasks_filter_by_tags(self, coordination_service: CoordinationService):
        """Filter tasks that contain all specified tags."""
        t1 = await coordination_service.create_task(
            title="Tagged Task", agent="agent", tags=["research", "api"]
        )
        t2 = await coordination_service.create_task(title="Other Task", agent="agent", tags=["api"])
        await coordination_service.create_task(title="No Tags", agent="agent")

        tasks = await coordination_service.list_tasks(tags=["research"])
        ids = [t["id"] for t in tasks]
        assert t1 in ids
        assert t2 not in ids

        tasks = await coordination_service.list_tasks(tags=["api"])
        ids = [t["id"] for t in tasks]
        assert t1 in ids
        assert t2 in ids

    @pytest.mark.asyncio
    async def test_list_tasks_filter_by_since(self, coordination_service: CoordinationService):
        """Filter tasks by created_at >= since."""
        import asyncio
        from datetime import timezone

        await coordination_service.create_task(title="Old Task", agent="agent")
        await asyncio.sleep(0.05)
        cutoff = datetime.now(timezone.utc).isoformat()
        await asyncio.sleep(0.05)
        new_id = await coordination_service.create_task(title="New Task", agent="agent")

        tasks = await coordination_service.list_tasks(since=cutoff)
        ids = [t["id"] for t in tasks]
        assert new_id in ids
        # Old task created before cutoff should not appear
        assert all(t["title"] != "Old Task" for t in tasks)

    @pytest.mark.asyncio
    async def test_list_tasks_returns_task_fields(self, coordination_service: CoordinationService):
        """Returned dicts include all expected fields."""
        task_id = await coordination_service.create_task(
            title="Full Task",
            agent="agent",
            description="A description",
            tags=["tag1"],
        )

        tasks = await coordination_service.list_tasks()
        task = next(t for t in tasks if t["id"] == task_id)

        assert task["title"] == "Full Task"
        assert task["description"] == "A description"
        assert task["status"] == "open"
        assert task["created_by"] == "agent"
        assert "tag1" in task["tags"]
        assert task["created_at"] is not None
        # Open tasks have no outcome yet but the key is present so consumers
        # don't have to do an existence-check on every row.
        assert task["outcome"] is None

    @pytest.mark.asyncio
    async def test_list_tasks_surfaces_outcome_after_completion(
        self, coordination_service: CoordinationService
    ):
        """Completed tasks expose the persisted outcome via list_tasks."""
        task_id = await coordination_service.create_task(
            title="Outcome Task",
            agent="agent",
        )
        await coordination_service.complete_task(task_id, "agent", outcome="ship it")

        tasks = await coordination_service.list_tasks(status="completed")
        task = next(t for t in tasks if t["id"] == task_id)
        assert task["outcome"] == "ship it"

    @pytest.mark.asyncio
    async def test_list_tasks_without_with_claims_omits_claims_field(
        self, coordination_service: CoordinationService
    ):
        """By default list_tasks does not attach a claims field."""
        task_id = await coordination_service.create_task(title="Plain", agent="agent")
        await coordination_service.claim_task(task_id, "work", "agent")

        tasks = await coordination_service.list_tasks()
        task = next(t for t in tasks if t["id"] == task_id)
        assert "claims" not in task

    @pytest.mark.asyncio
    async def test_list_tasks_with_claims_inlines_active_claims(
        self, coordination_service: CoordinationService
    ):
        """with_claims=True attaches the same claim shape lithos_task_status emits."""
        claimed_id = await coordination_service.create_task(title="Claimed", agent="agent")
        await coordination_service.claim_task(
            claimed_id, "implementation", "worker-a", ttl_minutes=10
        )
        unclaimed_id = await coordination_service.create_task(title="Unclaimed", agent="agent")

        tasks = await coordination_service.list_tasks(with_claims=True)
        by_id = {t["id"]: t for t in tasks}

        assert "claims" in by_id[claimed_id]
        assert by_id[claimed_id]["claims"] == [
            {
                "agent": "worker-a",
                "aspect": "implementation",
                "expires_at": by_id[claimed_id]["claims"][0]["expires_at"],
            }
        ]
        # And unclaimed tasks get an empty list, not a missing key.
        assert by_id[unclaimed_id]["claims"] == []

    @pytest.mark.asyncio
    async def test_list_tasks_with_claims_excludes_expired(
        self, coordination_service: CoordinationService
    ):
        """Expired claims are filtered out, matching lithos_task_status semantics."""
        task_id = await coordination_service.create_task(title="Expiry", agent="agent")
        await coordination_service.claim_task(task_id, "work", "agent", ttl_minutes=10)

        # Force-expire the claim by rewriting its expires_at directly.
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        async with aiosqlite.connect(coordination_service.db_path) as db:
            await db.execute("UPDATE claims SET expires_at = ? WHERE task_id = ?", (past, task_id))
            await db.commit()

        tasks = await coordination_service.list_tasks(with_claims=True)
        task = next(t for t in tasks if t["id"] == task_id)
        assert task["claims"] == []

    @pytest.mark.asyncio
    async def test_list_tasks_filter_by_resolved_since_includes_completed_and_cancelled(
        self, coordination_service: CoordinationService
    ):
        """resolved_since returns terminal tasks (both completed and cancelled).

        This is the core use case from #286: SSE consumers replaying recent
        terminal state on restart need a single query that covers both
        ``complete`` and ``cancel`` resolutions.
        """
        import asyncio

        open_id = await coordination_service.create_task(title="Open", agent="agent")
        complete_id = await coordination_service.create_task(title="Will Complete", agent="agent")
        cancel_id = await coordination_service.create_task(title="Will Cancel", agent="agent")

        cutoff = datetime.now(timezone.utc).isoformat()
        await asyncio.sleep(0.05)

        await coordination_service.complete_task(complete_id, "agent", outcome="done")
        await coordination_service.cancel_task(cancel_id, "agent")

        tasks = await coordination_service.list_tasks(resolved_since=cutoff)
        ids = {t["id"] for t in tasks}
        assert complete_id in ids
        assert cancel_id in ids
        assert open_id not in ids

    @pytest.mark.asyncio
    async def test_list_tasks_filter_by_resolved_since_excludes_null_resolved_at(
        self, coordination_service: CoordinationService
    ):
        """Rows with resolved_at IS NULL are excluded by the resolved_since filter.

        Covers two row shapes that legitimately have NULL resolved_at after
        migration: (a) open tasks that never reached a terminal transition,
        and (b) tasks cancelled before the dual-write was added (we simulate
        the latter by manually NULLing resolved_at after cancel).
        """
        open_id = await coordination_service.create_task(title="Still Open", agent="agent")

        legacy_cancel_id = await coordination_service.create_task(
            title="Pre-dual-write cancel", agent="agent"
        )
        await coordination_service.cancel_task(legacy_cancel_id, "agent")
        # Simulate the historical state where cancel_task did not write resolved_at.
        async with aiosqlite.connect(coordination_service.db_path) as db:
            await db.execute(
                "UPDATE tasks SET resolved_at = NULL WHERE id = ?",
                (legacy_cancel_id,),
            )
            await db.commit()

        cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        tasks = await coordination_service.list_tasks(resolved_since=cutoff)
        ids = {t["id"] for t in tasks}
        assert open_id not in ids
        assert legacy_cancel_id not in ids

    @pytest.mark.asyncio
    async def test_list_tasks_returns_resolved_at_key(
        self, coordination_service: CoordinationService
    ):
        """list_tasks payload exposes resolved_at so consumers can sort/group.

        Open tasks: resolved_at is None.
        Completed tasks: resolved_at carries an ISO timestamp string.
        """
        open_id = await coordination_service.create_task(title="Open", agent="agent")
        done_id = await coordination_service.create_task(title="Done", agent="agent")
        await coordination_service.complete_task(done_id, "agent", outcome="ok")

        tasks = await coordination_service.list_tasks()
        by_id = {t["id"]: t for t in tasks}

        assert "resolved_at" in by_id[open_id]
        assert by_id[open_id]["resolved_at"] is None

        assert "resolved_at" in by_id[done_id]
        assert by_id[done_id]["resolved_at"] is not None


class TestCoordinationStats:
    """Tests for coordination statistics."""

    @pytest.mark.asyncio
    async def test_get_stats(self, coordination_service: CoordinationService):
        """Get coordination statistics."""
        # Create some data
        await coordination_service.register_agent("stats-agent")
        task_id = await coordination_service.create_task(
            title="Stats Task",
            agent="stats-agent",
        )
        await coordination_service.claim_task(task_id, "work", "stats-agent")

        stats = await coordination_service.get_stats()

        assert "agents" in stats
        assert "active_tasks" in stats
        assert "open_claims" in stats
        assert stats["agents"] >= 1
        assert stats["active_tasks"] >= 1
        assert stats["open_claims"] >= 1


class TestTaskUpdate:
    """Tests for update_task partial-update method."""

    @pytest.mark.asyncio
    async def test_update_title(self, coordination_service: CoordinationService):
        """Update task title."""
        task_id = await coordination_service.create_task(
            title="Original Title",
            agent="agent",
        )
        success = await coordination_service.update_task(task_id, "agent", title="New Title")
        assert success
        task = await coordination_service.get_task(task_id)
        assert task is not None
        assert task.title == "New Title"

    @pytest.mark.asyncio
    async def test_update_description(self, coordination_service: CoordinationService):
        """Update task description."""
        task_id = await coordination_service.create_task(
            title="Task",
            agent="agent",
            description="Old description",
        )
        success = await coordination_service.update_task(
            task_id, "agent", description="New description"
        )
        assert success
        task = await coordination_service.get_task(task_id)
        assert task is not None
        assert task.description == "New description"

    @pytest.mark.asyncio
    async def test_update_tags(self, coordination_service: CoordinationService):
        """Update task tags."""
        task_id = await coordination_service.create_task(
            title="Task",
            agent="agent",
            tags=["old"],
        )
        success = await coordination_service.update_task(task_id, "agent", tags=["new", "updated"])
        assert success
        task = await coordination_service.get_task(task_id)
        assert task is not None
        assert task.tags == ["new", "updated"]

    @pytest.mark.asyncio
    async def test_update_multiple_fields(self, coordination_service: CoordinationService):
        """Update title, description, and tags in one call."""
        task_id = await coordination_service.create_task(
            title="Task",
            agent="agent",
            description="Old",
            tags=["a"],
        )
        success = await coordination_service.update_task(
            task_id,
            "agent",
            title="Updated",
            description="New",
            tags=["b", "c"],
        )
        assert success
        task = await coordination_service.get_task(task_id)
        assert task is not None
        assert task.title == "Updated"
        assert task.description == "New"
        assert task.tags == ["b", "c"]

    @pytest.mark.asyncio
    async def test_update_nonexistent_task_returns_false(
        self, coordination_service: CoordinationService
    ):
        """update_task returns False for unknown task_id."""
        success = await coordination_service.update_task("nonexistent-id", "agent", title="Nope")
        assert not success

    @pytest.mark.asyncio
    async def test_update_does_not_change_status(self, coordination_service: CoordinationService):
        """update_task does not alter task status."""
        task_id = await coordination_service.create_task(title="Task", agent="agent")
        await coordination_service.update_task(task_id, "agent", title="Changed")
        task = await coordination_service.get_task(task_id)
        assert task is not None
        assert task.status == "open"


class TestTaskOutcomeMigration:
    """Verify ALTER-TABLE migrations add outcome/resolved_at to pre-existing DBs."""

    @pytest.mark.asyncio
    async def test_migration_adds_outcome_and_resolved_at_columns(self, tmp_path):
        """Simulate a pre-#178 coordination.db and confirm initialize() migrates it.

        Pre-#178 databases lack both ``outcome`` and the resolution timestamp.
        After ``initialize()`` they should have ``outcome`` and ``resolved_at``
        (the latter via the consolidated ``_migrate_tasks_ensure_resolved_at``
        migration, which adds the column from scratch when neither
        ``completed_at`` nor ``resolved_at`` is present).
        """
        db_path = tmp_path / "coordination.db"

        # Build a legacy tasks table with the pre-#178 schema (no outcome/resolved_at).
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT DEFAULT 'open',
                    created_by TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    tags JSON
                )
                """
            )
            await db.execute(
                "INSERT INTO tasks (id, title, created_by) VALUES (?, ?, ?)",
                ("legacy-task", "Legacy", "legacy-agent"),
            )
            await db.commit()

        config = LithosConfig(storage=StorageConfig(data_dir=tmp_path))
        service = CoordinationService(config=config)
        service._db_path = db_path
        await service.initialize()

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("PRAGMA table_info(tasks)")
            columns = {row[1] for row in await cursor.fetchall()}

        assert "outcome" in columns
        assert "resolved_at" in columns
        assert "completed_at" not in columns

        # Legacy row still readable and reports None for the new fields.
        task = await service.get_task("legacy-task")
        assert task is not None
        assert task.outcome is None
        assert task.resolved_at is None

        # Completing the legacy task works and persists outcome on the migrated schema.
        success = await service.complete_task("legacy-task", "legacy-agent", outcome="done")
        assert success
        task = await service.get_task("legacy-task")
        assert task is not None
        assert task.outcome == "done"
        assert task.resolved_at is not None

    @pytest.mark.asyncio
    async def test_migration_renames_completed_at_to_resolved_at(self, tmp_path):
        """A current-main DB with completed_at is renamed to resolved_at in place.

        This is the common production upgrade path. The fixture pre-populates a
        completed row (resolved_at populated) and a cancelled row (resolved_at
        NULL — the historical state where cancel_task never wrote the column).
        Both rows must survive the migration with their data preserved.
        """
        db_path = tmp_path / "coordination.db"

        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT DEFAULT 'open',
                    created_by TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    tags JSON,
                    outcome TEXT,
                    completed_at TIMESTAMP,
                    metadata JSON
                )
                """
            )
            done_at = "2025-01-01T12:00:00+00:00"
            await db.execute(
                "INSERT INTO tasks (id, title, status, created_by, outcome, completed_at) "
                "VALUES (?, ?, 'completed', ?, ?, ?)",
                ("done-task", "Already done", "old-agent", "shipped", done_at),
            )
            await db.execute(
                "INSERT INTO tasks (id, title, status, created_by) VALUES (?, ?, 'cancelled', ?)",
                ("cancelled-task", "Already cancelled", "old-agent"),
            )
            await db.commit()

        config = LithosConfig(storage=StorageConfig(data_dir=tmp_path))
        service = CoordinationService(config=config)
        service._db_path = db_path
        await service.initialize()

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("PRAGMA table_info(tasks)")
            columns = {row[1] for row in await cursor.fetchall()}
        assert "resolved_at" in columns
        assert "completed_at" not in columns

        done = await service.get_task("done-task")
        assert done is not None
        assert done.status == "completed"
        assert done.outcome == "shipped"
        assert done.resolved_at is not None
        assert done.resolved_at.isoformat().startswith("2025-01-01T12:00:00")

        cancelled = await service.get_task("cancelled-task")
        assert cancelled is not None
        assert cancelled.status == "cancelled"
        # Historical cancellation: predates the dual-write so resolved_at stays NULL.
        assert cancelled.resolved_at is None

    @pytest.mark.asyncio
    async def test_migration_is_idempotent(self, tmp_path):
        """Re-running initialize() against a migrated DB is a no-op.

        Specifically: the resolved_at column does not disappear, and no
        spurious completed_at column reappears. This guards against future
        regressions where the migration logic might inadvertently strip or
        re-create columns.
        """
        db_path = tmp_path / "coordination.db"
        config = LithosConfig(storage=StorageConfig(data_dir=tmp_path))
        service = CoordinationService(config=config)
        service._db_path = db_path

        # First initialize from clean state — sets up the canonical schema.
        await service.initialize()
        # Second initialize against the already-migrated DB.
        await service.initialize()

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("PRAGMA table_info(tasks)")
            columns = {row[1] for row in await cursor.fetchall()}
        assert "resolved_at" in columns
        assert "completed_at" not in columns

    @pytest.mark.asyncio
    async def test_migration_both_columns_backfills_resolved_at(self, tmp_path):
        """Defensive branch: if both columns exist, backfill resolved_at from completed_at.

        Without the backfill, rows whose terminal timestamp lives only in
        ``completed_at`` would silently vanish from
        ``lithos_task_list(resolved_since=...)`` and ``get_task().resolved_at``,
        because every read path on the new schema looks at ``resolved_at``
        only. The migration's defensive branch must reconcile the two
        columns before returning so the public surface stays consistent
        with the underlying data.
        """
        db_path = tmp_path / "coordination.db"

        async with aiosqlite.connect(db_path) as db:
            # Build a tasks table with BOTH columns — the defensive state.
            await db.execute(
                """
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT DEFAULT 'open',
                    created_by TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    tags JSON,
                    outcome TEXT,
                    completed_at TIMESTAMP,
                    resolved_at TIMESTAMP,
                    metadata JSON
                )
                """
            )
            legacy_ts = "2025-02-15T09:30:00+00:00"
            modern_ts = "2025-03-01T14:00:00+00:00"
            # Row A: only completed_at populated — vulnerable to silent loss.
            await db.execute(
                "INSERT INTO tasks (id, title, status, created_by, completed_at) "
                "VALUES (?, ?, 'completed', ?, ?)",
                ("legacy-only", "Legacy-shaped row", "old-agent", legacy_ts),
            )
            # Row B: only resolved_at populated — already canonical.
            await db.execute(
                "INSERT INTO tasks (id, title, status, created_by, resolved_at) "
                "VALUES (?, ?, 'completed', ?, ?)",
                ("modern-only", "Modern-shaped row", "new-agent", modern_ts),
            )
            # Row C: both populated — resolved_at must win, completed_at preserved.
            await db.execute(
                "INSERT INTO tasks (id, title, status, created_by, completed_at, resolved_at) "
                "VALUES (?, ?, 'completed', ?, ?, ?)",
                ("both", "Both-populated row", "mixed-agent", legacy_ts, modern_ts),
            )
            # Row D: open task — both NULL, must stay NULL.
            await db.execute(
                "INSERT INTO tasks (id, title, created_by) VALUES (?, ?, ?)",
                ("open-task", "Open", "open-agent"),
            )
            await db.commit()

        config = LithosConfig(storage=StorageConfig(data_dir=tmp_path))
        service = CoordinationService(config=config)
        service._db_path = db_path
        await service.initialize()

        # completed_at is preserved for forensic inspection.
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("PRAGMA table_info(tasks)")
            columns = {row[1] for row in await cursor.fetchall()}
        assert "resolved_at" in columns
        assert "completed_at" in columns

        # Row A: backfilled from completed_at — no longer silently invisible.
        legacy_task = await service.get_task("legacy-only")
        assert legacy_task is not None
        assert legacy_task.resolved_at is not None
        assert legacy_task.resolved_at.isoformat().startswith("2025-02-15T09:30:00")

        # Row B: untouched — already canonical.
        modern_task = await service.get_task("modern-only")
        assert modern_task is not None
        assert modern_task.resolved_at is not None
        assert modern_task.resolved_at.isoformat().startswith("2025-03-01T14:00:00")

        # Row C: resolved_at wins over completed_at (no overwrite of populated values).
        both_task = await service.get_task("both")
        assert both_task is not None
        assert both_task.resolved_at is not None
        assert both_task.resolved_at.isoformat().startswith("2025-03-01T14:00:00")

        # Row D: open task stays NULL.
        open_task = await service.get_task("open-task")
        assert open_task is not None
        assert open_task.resolved_at is None

        # The new query surface now finds rows A, B, and C — the rescue worked.
        cutoff = "2025-01-01T00:00:00+00:00"
        tasks = await service.list_tasks(resolved_since=cutoff)
        ids = {t["id"] for t in tasks}
        assert "legacy-only" in ids
        assert "modern-only" in ids
        assert "both" in ids
        assert "open-task" not in ids

    @pytest.mark.asyncio
    async def test_migration_preserves_row_count(self, tmp_path):
        """The rename migration must not lose rows.

        SQLite's RENAME COLUMN guarantees this at the storage level; the test
        protects the invariant for future migration evolution (e.g. if someone
        replaces it with a create-new-table-copy-rows dance).
        """
        db_path = tmp_path / "coordination.db"

        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT DEFAULT 'open',
                    created_by TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    tags JSON,
                    outcome TEXT,
                    completed_at TIMESTAMP,
                    metadata JSON
                )
                """
            )
            for i in range(7):
                await db.execute(
                    "INSERT INTO tasks (id, title, created_by) VALUES (?, ?, ?)",
                    (f"task-{i}", f"Task {i}", "row-count-agent"),
                )
            await db.commit()

        config = LithosConfig(storage=StorageConfig(data_dir=tmp_path))
        service = CoordinationService(config=config)
        service._db_path = db_path
        await service.initialize()

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM tasks")
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 7


class TestTaskMetadata:
    """Tests for metadata JSON field on tasks (#215)."""

    @pytest.mark.asyncio
    async def test_create_task_with_metadata(self, coordination_service: CoordinationService):
        """create_task(metadata=...) persists the metadata dict."""
        meta = {"priority": "high", "source": "forge", "count": 42}
        task_id = await coordination_service.create_task(
            title="Task With Metadata",
            agent="agent",
            metadata=meta,
        )

        task = await coordination_service.get_task(task_id)
        assert task is not None
        assert task.metadata == meta

    @pytest.mark.asyncio
    async def test_create_task_without_metadata_defaults_empty(
        self, coordination_service: CoordinationService
    ):
        """Omitting metadata is backward-compatible — task.metadata is {}."""
        task_id = await coordination_service.create_task(
            title="Task Without Metadata",
            agent="agent",
        )

        task = await coordination_service.get_task(task_id)
        assert task is not None
        assert task.metadata == {}

    @pytest.mark.asyncio
    async def test_list_tasks_includes_metadata(self, coordination_service: CoordinationService):
        """list_tasks response dicts include the metadata field."""
        meta = {"env": "test"}
        task_id = await coordination_service.create_task(
            title="Listed Task",
            agent="agent",
            metadata=meta,
        )

        tasks = await coordination_service.list_tasks()
        task_dict = next((t for t in tasks if t["id"] == task_id), None)
        assert task_dict is not None
        assert "metadata" in task_dict
        assert task_dict["metadata"] == meta

    @pytest.mark.asyncio
    async def test_list_tasks_no_metadata_returns_empty_dict(
        self, coordination_service: CoordinationService
    ):
        """Tasks created without metadata return {} in list_tasks, not null."""
        task_id = await coordination_service.create_task(
            title="No Meta Task",
            agent="agent",
        )

        tasks = await coordination_service.list_tasks()
        task_dict = next((t for t in tasks if t["id"] == task_id), None)
        assert task_dict is not None
        assert task_dict["metadata"] == {}

    @pytest.mark.asyncio
    async def test_get_task_status_includes_metadata(
        self, coordination_service: CoordinationService
    ):
        """get_task_status response includes metadata from the task."""
        meta = {"sprint": 7, "team": "alpha"}
        task_id = await coordination_service.create_task(
            title="Status Task",
            agent="agent",
            metadata=meta,
        )

        statuses = await coordination_service.get_task_status(task_id)
        assert len(statuses) == 1
        assert statuses[0].metadata == meta

    @pytest.mark.asyncio
    async def test_get_task_status_includes_all_task_fields(
        self, coordination_service: CoordinationService
    ):
        """get_task_status surfaces description, tags, created_by, created_at,
        resolved_at, outcome — fields that earlier dropped silently."""
        meta = {"priority": "high"}
        task_id = await coordination_service.create_task(
            title="Full Status",
            agent="creator-agent",
            description="A full description",
            tags=["loom", "demo"],
            metadata=meta,
        )

        # Open task: outcome and resolved_at are None, the rest are populated.
        [open_status] = await coordination_service.get_task_status(task_id)
        assert open_status.description == "A full description"
        assert open_status.created_by == "creator-agent"
        assert open_status.created_at is not None
        assert open_status.tags == ["loom", "demo"]
        assert open_status.metadata == meta
        assert open_status.outcome is None
        assert open_status.resolved_at is None

        # Completed task: outcome and resolved_at are populated.
        await coordination_service.complete_task(task_id, "creator-agent", outcome="shipped")
        [done_status] = await coordination_service.get_task_status(task_id)
        assert done_status.outcome == "shipped"
        assert done_status.resolved_at is not None
        assert done_status.tags == ["loom", "demo"]
        assert done_status.metadata == meta

    @pytest.mark.asyncio
    async def test_migration_adds_metadata_column(self, tmp_path):
        """Simulate a pre-#215 coordination.db and confirm initialize() migrates it."""
        db_path = tmp_path / "coordination.db"

        # Build a legacy tasks table without metadata column.
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT DEFAULT 'open',
                    created_by TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    tags JSON,
                    outcome TEXT,
                    completed_at TIMESTAMP
                )
                """
            )
            await db.execute(
                "INSERT INTO tasks (id, title, created_by) VALUES (?, ?, ?)",
                ("legacy-task-215", "Legacy", "legacy-agent"),
            )
            await db.commit()

        config = LithosConfig(storage=StorageConfig(data_dir=tmp_path))
        service = CoordinationService(config=config)
        service._db_path = db_path
        await service.initialize()

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("PRAGMA table_info(tasks)")
            columns = {row[1] for row in await cursor.fetchall()}

        assert "metadata" in columns

        # Legacy row returns {} for metadata (not null).
        task = await service.get_task("legacy-task-215")
        assert task is not None
        assert task.metadata == {}


class TestMergeMetadataHelper:
    """Tests for the shared merge_metadata pure helper (#290, #305)."""

    def test_empty_patch_returns_copy_of_existing(self):
        from lithos._merge import merge_metadata

        existing = {"a": 1, "b": 2}
        result = merge_metadata(existing, {})
        assert result == {"a": 1, "b": 2}
        # Pure function: must not mutate inputs and must return a new dict.
        assert result is not existing

    def test_set_new_key(self):
        from lithos._merge import merge_metadata

        assert merge_metadata({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_overwrite_existing_key(self):
        from lithos._merge import merge_metadata

        assert merge_metadata({"a": 1, "b": 2}, {"a": 99}) == {"a": 99, "b": 2}

    def test_null_deletes_key(self):
        from lithos._merge import merge_metadata

        assert merge_metadata({"a": 1, "b": 2}, {"a": None}) == {"b": 2}

    def test_null_for_absent_key_is_silent_noop(self):
        from lithos._merge import merge_metadata

        assert merge_metadata({"a": 1}, {"absent": None}) == {"a": 1}

    def test_combined_set_and_delete(self):
        from lithos._merge import merge_metadata

        assert merge_metadata({"a": 1, "b": 2}, {"a": None, "c": 3}) == {"b": 2, "c": 3}


class TestTaskUpdateMetadataMerge:
    """Tests for additive per-key metadata merge on update_task (#290)."""

    @pytest.mark.asyncio
    async def test_merge_sets_new_key_preserves_existing(
        self, coordination_service: CoordinationService
    ):
        task_id = await coordination_service.create_task(
            title="Merge New Key",
            agent="agent",
            metadata={"a": 1, "b": 2},
        )

        updated = await coordination_service.update_task(
            task_id=task_id, agent="agent", metadata={"c": 3}
        )
        assert updated

        task = await coordination_service.get_task(task_id)
        assert task is not None
        assert task.metadata == {"a": 1, "b": 2, "c": 3}

    @pytest.mark.asyncio
    async def test_merge_overwrites_existing_key_preserves_others(
        self, coordination_service: CoordinationService
    ):
        task_id = await coordination_service.create_task(
            title="Merge Overwrite",
            agent="agent",
            metadata={"a": 1, "b": 2},
        )

        updated = await coordination_service.update_task(
            task_id=task_id, agent="agent", metadata={"a": 99}
        )
        assert updated

        task = await coordination_service.get_task(task_id)
        assert task is not None
        assert task.metadata == {"a": 99, "b": 2}

    @pytest.mark.asyncio
    async def test_merge_null_deletes_key(self, coordination_service: CoordinationService):
        task_id = await coordination_service.create_task(
            title="Merge Delete",
            agent="agent",
            metadata={"a": 1, "b": 2},
        )

        updated = await coordination_service.update_task(
            task_id=task_id, agent="agent", metadata={"a": None}
        )
        assert updated

        task = await coordination_service.get_task(task_id)
        assert task is not None
        assert task.metadata == {"b": 2}

    @pytest.mark.asyncio
    async def test_merge_combined_set_and_delete_in_one_call(
        self, coordination_service: CoordinationService
    ):
        task_id = await coordination_service.create_task(
            title="Merge Combined",
            agent="agent",
            metadata={"a": 1, "b": 2},
        )

        updated = await coordination_service.update_task(
            task_id=task_id,
            agent="agent",
            metadata={"a": None, "c": 3},
        )
        assert updated

        task = await coordination_service.get_task(task_id)
        assert task is not None
        assert task.metadata == {"b": 2, "c": 3}

    @pytest.mark.asyncio
    async def test_merge_empty_dict_is_noop(self, coordination_service: CoordinationService):
        """metadata={} preserves all existing keys (no wholesale-clear affordance)."""
        task_id = await coordination_service.create_task(
            title="Merge Empty Patch",
            agent="agent",
            metadata={"a": 1, "b": 2},
        )

        updated = await coordination_service.update_task(
            task_id=task_id, agent="agent", metadata={}
        )
        # Task exists and is open, so the call still reports success.
        assert updated

        task = await coordination_service.get_task(task_id)
        assert task is not None
        assert task.metadata == {"a": 1, "b": 2}

    @pytest.mark.asyncio
    async def test_merge_into_initially_empty_metadata(
        self, coordination_service: CoordinationService
    ):
        """Patch into a task created with no metadata (NULL column) yields the patch."""
        task_id = await coordination_service.create_task(
            title="Merge From Empty",
            agent="agent",
        )

        updated = await coordination_service.update_task(
            task_id=task_id, agent="agent", metadata={"a": 1}
        )
        assert updated

        task = await coordination_service.get_task(task_id)
        assert task is not None
        assert task.metadata == {"a": 1}

    @pytest.mark.asyncio
    async def test_merge_null_for_missing_key_is_silent(
        self, coordination_service: CoordinationService
    ):
        """Deleting a key that isn't present is a silent no-op, not an error."""
        task_id = await coordination_service.create_task(
            title="Merge Delete Missing",
            agent="agent",
            metadata={"a": 1},
        )

        updated = await coordination_service.update_task(
            task_id=task_id, agent="agent", metadata={"absent": None}
        )
        assert updated

        task = await coordination_service.get_task(task_id)
        assert task is not None
        assert task.metadata == {"a": 1}

    @pytest.mark.asyncio
    async def test_merge_on_completed_task_returns_false(
        self, coordination_service: CoordinationService
    ):
        """Completed tasks are treated as not-found; metadata is not mutated."""
        task_id = await coordination_service.create_task(
            title="Completed Task",
            agent="agent",
            metadata={"a": 1},
        )
        await coordination_service.complete_task(task_id, agent="agent", outcome="done")

        updated = await coordination_service.update_task(
            task_id=task_id, agent="agent", metadata={"b": 2}
        )
        assert updated is False

        task = await coordination_service.get_task(task_id)
        assert task is not None
        assert task.metadata == {"a": 1}

    @pytest.mark.asyncio
    async def test_merge_preserves_other_columns(self, coordination_service: CoordinationService):
        """Title and metadata merge in the same call both take effect."""
        task_id = await coordination_service.create_task(
            title="Original Title",
            agent="agent",
            metadata={"a": 1},
        )

        updated = await coordination_service.update_task(
            task_id=task_id,
            agent="agent",
            title="New Title",
            metadata={"b": 2},
        )
        assert updated

        task = await coordination_service.get_task(task_id)
        assert task is not None
        assert task.title == "New Title"
        assert task.metadata == {"a": 1, "b": 2}

    @pytest.mark.parametrize(
        "stored_raw",
        ["null", "[1, 2]", '"x"', "42", "true"],
        ids=["null", "array", "string", "number", "bool"],
    )
    @pytest.mark.asyncio
    async def test_merge_resilient_to_non_dict_stored_metadata(
        self, coordination_service: CoordinationService, stored_raw: str
    ):
        """Merge degrades to {} when stored metadata is valid JSON but not an object.

        Regression for the Copilot/reviewer finding on #291: ``json.loads(null)``,
        arrays, and scalars all decode to non-dict values that would otherwise
        crash ``_merge_metadata`` via ``dict(existing)``. The decode helper
        treats any non-object as empty and logs a warning; the merge proceeds.
        """
        task_id = await coordination_service.create_task(
            title="Corrupt Meta Task",
            agent="agent",
            metadata={"will_be": "clobbered"},
        )

        # Force on-disk corruption by writing a non-object JSON directly.
        async with aiosqlite.connect(coordination_service.db_path) as db:
            await db.execute(
                "UPDATE tasks SET metadata = ? WHERE id = ?",
                (stored_raw, task_id),
            )
            await db.commit()

        updated = await coordination_service.update_task(
            task_id=task_id, agent="agent", metadata={"new": 1}
        )
        assert updated

        task = await coordination_service.get_task(task_id)
        assert task is not None
        # Corrupt stored value collapsed to {} before the merge; patch
        # applies cleanly on top of the empty dict.
        assert task.metadata == {"new": 1}

    @pytest.mark.asyncio
    async def test_merge_concurrent_writers_do_not_clobber(
        self, coordination_service: CoordinationService
    ):
        """Two concurrent updates writing different keys both land in the final state.

        Regression guard for the multi-writer property that motivates #290:
        without BEGIN IMMEDIATE serialisation, two callers could both pass
        the SELECT and the loser's write would clobber the winner's key.
        """
        task_id = await coordination_service.create_task(
            title="Concurrent Writers",
            agent="agent",
            metadata={"base": "seed"},
        )

        results = await asyncio.gather(
            coordination_service.update_task(
                task_id=task_id, agent="writer-a", metadata={"a": "from-a"}
            ),
            coordination_service.update_task(
                task_id=task_id, agent="writer-b", metadata={"b": "from-b"}
            ),
        )
        assert all(results)

        task = await coordination_service.get_task(task_id)
        assert task is not None
        # Both keys must survive — neither writer clobbered the other, and
        # the original "base" key is preserved.
        assert task.metadata == {"base": "seed", "a": "from-a", "b": "from-b"}


class TestParseDatetimeWarnsOnFailure:
    """Regression for #205: silent None on parse failure hid data corruption."""

    def test_returns_none_and_warns_on_unparseable_value(self, caplog):
        """An unparseable string still returns None but emits a WARNING."""
        import logging

        from lithos.coordination import _parse_datetime

        with caplog.at_level(logging.WARNING, logger="lithos.coordination"):
            result = _parse_datetime("not-a-real-timestamp")

        assert result is None
        assert any(
            "Failed to parse datetime" in r.getMessage()
            and "not-a-real-timestamp" in r.getMessage()
            for r in caplog.records
        )

    def test_none_input_does_not_warn(self, caplog):
        """Legitimately-missing values stay silent — only corruption is logged."""
        import logging

        from lithos.coordination import _parse_datetime

        with caplog.at_level(logging.WARNING, logger="lithos.coordination"):
            result = _parse_datetime(None)

        assert result is None
        assert not any("Failed to parse datetime" in r.getMessage() for r in caplog.records)
