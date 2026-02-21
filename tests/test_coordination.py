"""Tests for coordination module - tasks, claims, agents, findings."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from exogram.coordination import CoordinationService


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
