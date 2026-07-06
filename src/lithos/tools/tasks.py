"""Task-coordination MCP tools: lifecycle, claiming, task graph."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

from lithos.coordination import Task, TaskStatus
from lithos.envelopes import error_envelope, invalid_input_envelope
from lithos.events import (
    TASK_CANCELLED,
    TASK_CLAIMED,
    TASK_COMPLETED,
    TASK_CREATED,
    TASK_RELEASED,
    TASK_REOPENED,
    TASK_UPDATED,
    LithosEvent,
)
from lithos.knowledge import validate_metadata_match
from lithos.telemetry import get_current_span, tool_metrics
from lithos.tools._seam import tool_span

if TYPE_CHECKING:
    from lithos.server import LithosServer

logger = logging.getLogger(__name__)


def _serialize_task_record(task: Task | TaskStatus) -> dict[str, Any]:
    """Render a Task or TaskStatus as the MCP wire-shape task dict.

    Used by both ``lithos_task_get`` (which serialises a ``Task``) and
    ``lithos_task_status`` (which serialises the task-shaped subset of a
    ``TaskStatus``, then layers ``claims`` on top). Centralising the field
    set + datetime formatting here stops the two responses drifting on
    additions or ISO serialisation choices.

    Claims are deliberately excluded — ``lithos_task_status`` adds them
    alongside this dict; ``lithos_task_get`` does not return them.
    """
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "task_type": task.task_type,
        "created_by": task.created_by,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "resolved_at": task.resolved_at.isoformat() if task.resolved_at else None,
        "tags": task.tags,
        "metadata": task.metadata,
        "outcome": task.outcome,
    }


def register(mcp: FastMCP, server: LithosServer) -> None:
    """Register the task tools. See the late-binding rule in :mod:`lithos.tools`."""

    @mcp.tool()
    @tool_metrics()
    @tool_span(map_coordination_error=True)
    async def lithos_task_create(
        title: str,
        agent: str,
        description: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        task_type: str = "task",
        depends_on: list[str] | None = None,
        parent_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new coordination task.

        Args:
            title: Task title
            agent: Creating agent identifier
            description: Task description
            tags: Task tags
            metadata: Arbitrary JSON metadata dict (optional). Must NOT contain
                ``depends_on``/``blocked_on`` — dependencies are first-class task
                edges now; pass ``depends_on`` instead.
            task_type: First-class task type: ``task``, ``epic``, or ``gate``.
                A ``gate`` is an external wait and requires
                ``metadata.gate_type`` in human/timer/ci/pr/external_task; a
                ``timer`` gate also requires a parseable ``metadata.ready_at``
                (ISO datetime). Link a task to a gate with a ``waits_on_gate``
                edge; resolve a gate by completing it (``timer`` gates resolve
                on their own once ``ready_at`` passes).
            depends_on: Predecessor task IDs. Each creates a ``blocks`` edge so
                this task is not ready until that predecessor is completed.
                Predecessors must already exist.
            parent_task_id: Optional parent. Creates a ``parent_child`` edge
                ``parent -> this task``. The parent must exist; it may be any
                task type (an ``epic`` or a plain ``task``).

        Returns:
            Dict with task_id, or an error envelope on validation failure.
        """
        logger.info("lithos_task_create agent=%s title=%r", agent, title)
        span = get_current_span()
        span.set_attribute("lithos.agent", agent)
        task_id = await server.coordination.create_task(
            title=title,
            agent=agent,
            description=description,
            tags=tags,
            metadata=metadata,
            task_type=task_type,
            depends_on=depends_on,
            parent_task_id=parent_task_id,
        )
        span.set_attribute("lithos.task_id", task_id)

        await server._emit(
            LithosEvent(
                type=TASK_CREATED,
                agent=agent,
                payload={"task_id": task_id, "title": title},
            )
        )

        return {"task_id": task_id}

    @mcp.tool()
    @tool_metrics()
    @tool_span(map_coordination_error=True)
    async def lithos_task_update(
        task_id: str,
        agent: str,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update mutable task fields (title, description, tags, metadata).

        At least one of title, description, tags, or metadata must be provided.
        Works on terminal (completed/cancelled) tasks too — useful for annotating
        an archived task (e.g. a metadata snapshot) without reviving it; use
        ``lithos_task_reopen`` to bring a task back to active work. ``task_not_found``
        now means the task genuinely does not exist.

        ``metadata`` is applied as an additive per-key merge: keys with non-null
        values overwrite the existing value, keys whose value is ``None`` are
        deleted from the existing metadata, and keys not mentioned are preserved.
        To clear a specific key, pass ``{"key": None}``. There is no
        wholesale-clear affordance — ``metadata={}`` is a no-op that preserves
        all existing keys.

        Args:
            task_id: Task ID to update
            agent: Agent making the update
            title: New task title (optional)
            description: New task description (optional)
            tags: New task tags (optional)
            metadata: Per-key merge patch into the existing metadata dict
                (optional). See merge contract above.

        Returns:
            Dict with success and message
        """
        if title is None and description is None and tags is None and metadata is None:
            return error_envelope(
                "invalid_input",
                "At least one of title, description, tags, or metadata must be provided",
            )

        logger.info("lithos_task_update task=%s agent=%s", task_id, agent)
        span = get_current_span()
        span.set_attribute("lithos.agent", agent)
        span.set_attribute("lithos.task_id", task_id)
        updated = await server.coordination.update_task(
            task_id=task_id,
            agent=agent,
            title=title,
            description=description,
            tags=tags,
            metadata=metadata,
        )
        span.set_attribute("lithos.success", updated)

        if updated:
            await server._emit(
                LithosEvent(
                    type=TASK_UPDATED,
                    agent=agent,
                    payload={"task_id": task_id},
                )
            )
            return {"success": True, "message": f"Task {task_id} updated"}
        return error_envelope("task_not_found", f"Task {task_id} not found")

    @mcp.tool()
    @tool_metrics()
    @tool_span(map_coordination_error=True)
    async def lithos_task_claim(
        task_id: str,
        aspect: str,
        agent: str,
        ttl_minutes: int = 60,
    ) -> dict[str, Any]:
        """Claim an aspect of a task.

        Args:
            task_id: Task ID
            aspect: Aspect being claimed (e.g., "research", "implementation")
            agent: Agent making the claim
            ttl_minutes: Claim duration in minutes (default: 60, max: 480)

        Returns:
            Dict with success and expires_at
        """
        logger.info("lithos_task_claim task=%s aspect=%s agent=%s", task_id, aspect, agent)
        span = get_current_span()
        span.set_attribute("lithos.agent", agent)
        span.set_attribute("lithos.task_id", task_id)
        span.set_attribute("lithos.aspect", aspect)
        success, expires_at = await server.coordination.claim_task(
            task_id=task_id,
            aspect=aspect,
            agent=agent,
            ttl_minutes=ttl_minutes,
        )
        span.set_attribute("lithos.success", success)

        if not success:
            return error_envelope(
                "claim_failed",
                f"Could not claim aspect '{aspect}' on task '{task_id}': "
                "task not found, not open, or aspect already claimed by another agent.",
            )

        await server._emit(
            LithosEvent(
                type=TASK_CLAIMED,
                agent=agent,
                payload={"task_id": task_id, "agent": agent, "aspect": aspect},
            )
        )

        return {
            "success": True,
            "expires_at": expires_at.isoformat(),  # type: ignore[union-attr]
        }

    @mcp.tool()
    @tool_metrics()
    @tool_span(map_coordination_error=True)
    async def lithos_task_renew(
        task_id: str,
        aspect: str,
        agent: str,
        ttl_minutes: int = 60,
    ) -> dict[str, Any]:
        """Renew an existing claim.

        Args:
            task_id: Task ID
            aspect: Claimed aspect
            agent: Agent that owns the claim
            ttl_minutes: New duration in minutes

        Returns:
            Dict with success and new_expires_at
        """
        logger.info("lithos_task_renew task=%s aspect=%s agent=%s", task_id, aspect, agent)
        span = get_current_span()
        span.set_attribute("lithos.agent", agent)
        span.set_attribute("lithos.task_id", task_id)
        span.set_attribute("lithos.aspect", aspect)
        success, new_expires = await server.coordination.renew_claim(
            task_id=task_id,
            aspect=aspect,
            agent=agent,
            ttl_minutes=ttl_minutes,
        )
        span.set_attribute("lithos.success", success)

        if not success:
            return error_envelope(
                "claim_not_found",
                f"No active claim found for task '{task_id}', aspect '{aspect}', agent '{agent}'.",
            )

        return {
            "success": True,
            "new_expires_at": new_expires.isoformat(),  # type: ignore[union-attr]
        }

    @mcp.tool()
    @tool_metrics()
    @tool_span(map_coordination_error=True)
    async def lithos_task_release(
        task_id: str,
        aspect: str,
        agent: str,
    ) -> dict[str, Any]:
        """Release a claim.

        Args:
            task_id: Task ID
            aspect: Claimed aspect
            agent: Agent releasing the claim

        Returns:
            Dict with success boolean, or error envelope if no matching claim
        """
        logger.info("lithos_task_release task=%s aspect=%s agent=%s", task_id, aspect, agent)
        span = get_current_span()
        span.set_attribute("lithos.agent", agent)
        span.set_attribute("lithos.task_id", task_id)
        span.set_attribute("lithos.aspect", aspect)
        success = await server.coordination.release_claim(
            task_id=task_id,
            aspect=aspect,
            agent=agent,
        )
        span.set_attribute("lithos.success", success)

        if not success:
            return error_envelope(
                "claim_not_found",
                f"No matching claim found for task '{task_id}', "
                f"aspect '{aspect}', agent '{agent}'.",
            )

        await server._emit(
            LithosEvent(
                type=TASK_RELEASED,
                agent=agent,
                payload={"task_id": task_id, "agent": agent, "aspect": aspect},
            )
        )

        return {"success": True}

    @mcp.tool()
    @tool_metrics()
    @tool_span(map_coordination_error=True)
    async def lithos_task_complete(
        task_id: str,
        agent: str,
        outcome: str | None = None,
        cited_nodes: list[str] | None = None,
        misleading_nodes: list[str] | None = None,
        receipt_id: str | None = None,
    ) -> dict[str, Any]:
        """Mark a task as completed.

        Args:
            task_id: Task ID
            agent: Agent completing the task
            outcome: Optional free-text completion summary. Persisted on
                the task row and forwarded in the ``task.completed`` event
                payload so LCMA consolidation can use it as the frame
                ``outcome`` slot.
            cited_nodes: Node IDs the agent found useful (None = no feedback)
            misleading_nodes: Node IDs the agent found misleading (None = no feedback)
            receipt_id: Specific receipt to bind feedback to (optional)

        Returns:
            Dict with success boolean, or error envelope if task not found or not open
        """
        outcome_len = len(outcome) if outcome else 0
        logger.info(
            "lithos_task_complete: called",
            extra={
                "task_id": task_id,
                "agent": agent,
                "outcome_provided": outcome is not None,
                "outcome_len": outcome_len,
                "cited_count": len(cited_nodes) if cited_nodes is not None else 0,
                "misleading_count": len(misleading_nodes) if misleading_nodes is not None else 0,
                "receipt_id": receipt_id,
            },
        )
        span = get_current_span()
        span.set_attribute("lithos.agent", agent)
        span.set_attribute("lithos.task_id", task_id)
        span.set_attribute("lithos.outcome_provided", outcome is not None)
        span.set_attribute("lithos.outcome_len", outcome_len)
        # -- Validate feedback BEFORE completing the task --
        feedback_supplied = cited_nodes is not None or misleading_nodes is not None
        validated: dict[str, Any] | None = None
        if feedback_supplied:
            error, validated = await server._validate_task_feedback(
                task_id=task_id,
                agent=agent,
                cited_nodes=cited_nodes,
                misleading_nodes=misleading_nodes,
                receipt_id=receipt_id,
            )
            if error is not None:
                return error

        success = await server.coordination.complete_task(
            task_id=task_id,
            agent=agent,
            outcome=outcome,
        )
        span.set_attribute("lithos.success", success)

        if not success:
            return error_envelope(
                "task_not_found", f"Task '{task_id}' not found or not in an open state."
            )

        # -- Apply reinforcement side-effects after task is completed --
        if validated is not None:
            logger.info(
                "lithos_task_complete: applying feedback reinforcement",
                extra={
                    "task_id": task_id,
                    "agent": agent,
                    "cited_count": len(validated.get("cited") or []),
                    "misleading_count": len(validated.get("misleading") or []),
                    "ignored_count": len(validated.get("ignored") or []),
                },
            )
            await server._apply_task_feedback(validated)

        await server._emit(
            LithosEvent(
                type=TASK_COMPLETED,
                agent=agent,
                payload={
                    "task_id": task_id,
                    "agent": agent,
                    "outcome": outcome,
                    "cited_nodes": json.dumps(cited_nodes),
                    "misleading_nodes": json.dumps(misleading_nodes),
                    "receipt_id": json.dumps(receipt_id),
                },
            )
        )

        # Surface tasks this completion just made ready (their last
        # blocking predecessor is now satisfied) so an orchestrator can
        # pick them up without re-polling lithos_task_ready.
        unblocked = await server.coordination.newly_unblocked_by(task_id)
        span.set_attribute("lithos.unblocked_count", len(unblocked))
        return {"success": True, "unblocked": unblocked}

    @mcp.tool()
    @tool_metrics()
    @tool_span(map_coordination_error=True)
    async def lithos_task_cancel(
        task_id: str,
        agent: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Cancel a task, releasing all claims.

        Args:
            task_id: Task ID
            agent: Agent cancelling the task
            reason: Optional reason for cancellation

        Returns:
            Dict with success boolean
        """
        logger.info("lithos_task_cancel task=%s agent=%s reason=%s", task_id, agent, reason)
        span = get_current_span()
        span.set_attribute("lithos.agent", agent)
        span.set_attribute("lithos.task_id", task_id)
        success = await server.coordination.cancel_task(
            task_id=task_id,
            agent=agent,
            reason=reason,
        )
        span.set_attribute("lithos.success", success)

        if success:
            await server._emit(
                LithosEvent(
                    type=TASK_CANCELLED,
                    agent=agent,
                    payload={"task_id": task_id, "agent": agent, "reason": reason},
                )
            )
            return {"success": True}

        return error_envelope("task_not_found", f"Task {task_id} not found or already closed")

    @mcp.tool()
    @tool_metrics()
    @tool_span(map_coordination_error=True)
    async def lithos_task_reopen(
        task_id: str,
        agent: str,
    ) -> dict[str, Any]:
        """Reopen a terminal (completed/cancelled) task back to ``open``.

        The inverse of complete/cancel — use it to revive a task to active work
        (e.g. an accidental completion) and to remediate dependents stranded as
        ``blocker_unsatisfiable`` by a cancelled blocker/gate: reopening that
        blocker/gate returns its dependents to a waiting state. Clears
        ``resolved_at``/``outcome``, records the reopen as a ``[Reopened]``
        finding, and emits a ``task.reopened`` event.

        Args:
            task_id: Task ID to reopen
            agent: Agent performing the reopen

        Returns:
            ``{"success": true, "reblocked": [...]}`` — ``reblocked`` lists open
            dependents this reopen put back under the task's block (non-empty
            only when reopening a *completed* blocker/gate; a cancelled-task
            reopen un-strands dependents and re-blocks no one). On failure,
            ``{"status": "error", "code": "task_not_found" | "task_not_resolved",
            "message": ...}``.
        """
        logger.info("lithos_task_reopen task=%s agent=%s", task_id, agent)
        span = get_current_span()
        span.set_attribute("lithos.agent", agent)
        span.set_attribute("lithos.task_id", task_id)
        prior_status, prior_outcome = await server.coordination.reopen_task(
            task_id=task_id, agent=agent
        )
        # Durable audit: a queryable finding recording the prior terminal state.
        summary = f"[Reopened] task reopened (was {prior_status})"
        if prior_outcome:
            summary += f"; prior outcome: {prior_outcome}"
        await server.coordination.post_finding(task_id=task_id, agent=agent, summary=summary)

        await server._emit(
            LithosEvent(
                type=TASK_REOPENED,
                agent=agent,
                payload={
                    "task_id": task_id,
                    "agent": agent,
                    "prior_status": prior_status,
                    "prior_outcome": prior_outcome,
                },
            )
        )
        reblocked = await server.coordination.newly_reblocked_by(task_id, prior_status)
        span.set_attribute("lithos.reblocked_count", len(reblocked))
        return {"success": True, "reblocked": reblocked}

    @mcp.tool()
    @tool_metrics()
    @tool_span(map_coordination_error=True)
    async def lithos_task_list(
        agent: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        since: str | None = None,
        resolved_since: str | None = None,
        with_claims: bool = False,
        metadata_match: dict | None = None,
        task_type: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """List tasks with optional filters.

        Args:
            agent: Filter by creating agent
            status: Filter by status: "open", "completed", or "cancelled" (None = all)
            task_type: Filter by first-class task type (task/epic/gate)
            tags: Filter by tags (task must have all specified tags)
            metadata_match: Filter by metadata (AND across keys). For each
                ``key: q`` a task matches when its stored metadata value
                equals ``q`` or is a list containing ``q``. Query values must
                be scalars (string/number/boolean); type-sensitive.
            since: Filter by created_at >= this ISO datetime string (e.g. "2024-01-01T00:00:00Z")
            resolved_since: Filter by resolved_at >= this ISO datetime string.
                ``resolved_at`` is set on both terminal transitions (complete
                and cancel), so this returns tasks resolved (in either way)
                within the window. Open tasks and historical cancellations
                from before the column was populated on cancel are excluded
                automatically (their ``resolved_at`` is NULL).
            with_claims: When True, each task in the response includes its
                active (non-expired) claims inline as a ``claims`` array
                (same shape as ``lithos_task_status``). Defaults to False.
                Use to avoid an N+1 of ``lithos_task_status`` calls when
                rendering a list view that needs claim info.

        Returns:
            Dict with tasks list containing id, title, description, status,
            created_by, created_at, resolved_at, tags, metadata, outcome,
            and (when with_claims) claims.
        """
        logger.info(
            "lithos_task_list agent=%s status=%s tags=%s since=%s resolved_since=%s with_claims=%s",
            agent,
            status,
            tags,
            since,
            resolved_since,
            with_claims,
        )
        span = get_current_span()
        if agent:
            span.set_attribute("lithos.agent", agent)
        if status:
            span.set_attribute("lithos.status", status)
        span.set_attribute("lithos.with_claims", with_claims)

        if metadata_match is not None:
            try:
                validate_metadata_match(metadata_match)
            except ValueError as e:
                return invalid_input_envelope(str(e))

        tasks = await server.coordination.list_tasks(
            agent=agent,
            status=status,
            tags=tags,
            since=since,
            resolved_since=resolved_since,
            with_claims=with_claims,
            metadata_match=metadata_match,
            task_type=task_type,
        )
        return {"tasks": tasks}

    @mcp.tool()
    @tool_metrics()
    @tool_span(map_coordination_error=True)
    async def lithos_task_edge_upsert(
        from_task_id: str,
        to_task_id: str,
        type: str,
        agent: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update a typed relation between two tasks.

        Edge types accepted in this phase: ``blocks`` (to_task is not ready
        until from_task is completed), ``parent_child`` (from_task is the
        parent; purely structural, never blocks), ``discovered_from`` (to_task
        was discovered while executing from_task; non-blocking), and
        ``waits_on_gate`` (to_task is not ready until the gate from_task is
        resolved — the gate is completed, or a ``timer`` gate whose
        ``ready_at`` has passed; a cancelled gate makes the waiter
        unsatisfiable).

        Args:
            from_task_id: Source task (blocker / parent / source).
            to_task_id: Target task (blocked / child / discovered).
            type: Edge type (see above).
            agent: Agent creating the edge.
            metadata: Optional edge metadata.

        Returns:
            ``{"success": true}`` or an error envelope (unaccepted type,
            self-edge, missing task, or a blocking edge that would create a
            dependency cycle).
        """
        logger.info(
            "lithos_task_edge_upsert from=%s to=%s type=%s agent=%s",
            from_task_id,
            to_task_id,
            type,
            agent,
        )
        span = get_current_span()
        span.set_attribute("lithos.agent", agent)
        span.set_attribute("lithos.edge_type", type)
        await server.coordination.upsert_task_edge(
            from_task_id=from_task_id,
            to_task_id=to_task_id,
            edge_type=type,
            agent=agent,
            metadata=metadata,
        )
        return {"success": True}

    @mcp.tool()
    @tool_metrics()
    @tool_span(map_coordination_error=True)
    async def lithos_task_edge_list(
        task_id: str,
        direction: str = "both",
        types: list[str] | None = None,
    ) -> dict[str, Any]:
        """List edges touching a task.

        Args:
            task_id: Task whose edges to list.
            direction: "incoming", "outgoing", or "both" (default).
            types: Optional edge-type filter.

        Returns:
            ``{"edges": [...]}`` — each edge carries from/to/type, its
            ``direction`` relative to ``task_id``, metadata, and provenance.
        """
        logger.info("lithos_task_edge_list task=%s direction=%s", task_id, direction)
        span = get_current_span()
        span.set_attribute("lithos.task_id", task_id)
        if direction not in ("incoming", "outgoing", "both"):
            return error_envelope(
                "invalid_input",
                f"direction must be 'incoming', 'outgoing', or 'both', got {direction!r}.",
            )
        edges = await server.coordination.list_task_edges(
            task_id=task_id,
            direction=direction,
            types=types,
        )
        return {"edges": edges}

    @mcp.tool()
    @tool_metrics()
    @tool_span(map_coordination_error=True)
    async def lithos_task_ready(
        project: str | None = None,
        tags: list[str] | None = None,
        metadata_match: dict | None = None,
        limit: int = 50,
        with_claims: bool = True,
    ) -> dict[str, Any]:
        """Return open tasks that are ready to work.

        A task is ready when it is open, not a gate/epic, has no incoming
        blocking edge whose predecessor is unsatisfied (every ``blocks``
        predecessor must be ``completed``), and is not blocked by a gate.
        Active claims are *attached* when ``with_claims`` but never used to
        exclude a task — claims are per-aspect and collision-safety comes
        from the atomic claim, so the picking agent decides what "taken" means.

        Args:
            project: Shorthand for ``metadata.project == project``.
            tags: Filter by tags (task must have all specified tags).
            metadata_match: Metadata filter (AND across keys); scalars only.
            limit: Maximum tasks to return.
            with_claims: Attach each task's active claims inline (default True).

        Returns:
            Dict with a ``tasks`` list (the feasible frontier).
        """
        logger.info(
            "lithos_task_ready project=%s tags=%s limit=%s with_claims=%s",
            project,
            tags,
            limit,
            with_claims,
        )
        span = get_current_span()
        if limit < 1:
            return error_envelope("invalid_input", f"limit must be >= 1, got {limit}.")
        if metadata_match is not None:
            try:
                validate_metadata_match(metadata_match)
            except ValueError as e:
                return invalid_input_envelope(str(e))
        tasks = await server.coordination.list_ready(
            project=project,
            tags=tags,
            metadata_match=metadata_match,
            limit=limit,
            with_claims=with_claims,
        )
        span.set_attribute("lithos.ready_count", len(tasks))
        return {"tasks": tasks}

    @mcp.tool()
    @tool_metrics()
    @tool_span(map_coordination_error=True)
    async def lithos_task_blocked(
        project: str | None = None,
        tags: list[str] | None = None,
        metadata_match: dict | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return open tasks that are NOT ready, with structured blocker reasons.

        Same filter surface as ``lithos_task_ready``. Each returned task carries
        a ``blockers`` list; each blocker has ``kind``:
        ``task`` (predecessor still open — just waiting),
        ``gate`` (waiting on an unresolved gate),
        ``blocker_unsatisfiable`` (predecessor or gate was cancelled — needs
        intervention), or ``cycle`` (the dependency chain forms a cycle).

        Args:
            project: Shorthand for ``metadata.project == project``.
            tags: Filter by tags (task must have all specified tags).
            metadata_match: Metadata filter (AND across keys); scalars only.
            limit: Maximum tasks to return.

        Returns:
            Dict with a ``tasks`` list, each task including its ``blockers``.
        """
        logger.info("lithos_task_blocked project=%s tags=%s limit=%s", project, tags, limit)
        span = get_current_span()
        if limit < 1:
            return error_envelope("invalid_input", f"limit must be >= 1, got {limit}.")
        if metadata_match is not None:
            try:
                validate_metadata_match(metadata_match)
            except ValueError as e:
                return invalid_input_envelope(str(e))
        tasks = await server.coordination.list_blocked(
            project=project,
            tags=tags,
            metadata_match=metadata_match,
            limit=limit,
        )
        span.set_attribute("lithos.blocked_count", len(tasks))
        return {"tasks": tasks}

    @mcp.tool()
    @tool_metrics()
    @tool_span(map_coordination_error=True)
    async def lithos_task_children(
        task_id: str,
        recursive: bool = False,
        include_closed: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return the child tasks of a parent/epic (via ``parent_child`` edges).

        Args:
            task_id: Parent (or epic) whose children to list.
            recursive: Walk the full descendant subtree, not just direct
                children (default False).
            include_closed: Include completed/cancelled children in the
                result (default False = open children only). The subtree is
                traversed in full regardless, so an open grandchild under a
                closed child is still surfaced.

        Returns:
            Dict with a ``tasks`` list (task records, same shape as
            ``lithos_task_list``).
        """
        logger.info(
            "lithos_task_children task=%s recursive=%s include_closed=%s",
            task_id,
            recursive,
            include_closed,
        )
        span = get_current_span()
        span.set_attribute("lithos.task_id", task_id)
        tasks = await server.coordination.list_children(
            task_id=task_id,
            recursive=recursive,
            include_closed=include_closed,
        )
        span.set_attribute("lithos.children_count", len(tasks))
        return {"tasks": tasks}

    @mcp.tool()
    @tool_metrics()
    @tool_span(map_coordination_error=True)
    async def lithos_task_spawn(
        source_task_id: str,
        title: str,
        agent: str,
        description: str | None = None,
        relation_type: str = "discovered_from",
        inherit_project: bool = True,
        inherit_tags: bool = True,
        inherit_context: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a follow-on task linked to an existing source task.

        The relation edge is always ``source -> spawned``:
        ``discovered_from`` records that the spawned task was found while
        executing the source (non-blocking); ``blocks`` makes the spawned task
        wait until the source is ``completed``.

        Args:
            source_task_id: The task this follow-on came from.
            title: Title for the spawned task.
            agent: Spawning agent identifier.
            description: Optional description for the spawned task.
            relation_type: ``discovered_from`` (default) or ``blocks``.
            inherit_project: Copy ``metadata.project`` from the source.
            inherit_tags: Copy the source's tags.
            inherit_context: Copy scheduling-convention metadata (priority,
                parallelizable, phase) from the source.
            metadata: Extra metadata; overrides inherited keys. Must NOT
                contain ``depends_on``/``blocked_on``.

        Returns:
            Dict with task_id, or an error envelope (unknown source, invalid
            relation_type, or forbidden metadata key).
        """
        logger.info(
            "lithos_task_spawn source=%s agent=%s relation=%s",
            source_task_id,
            agent,
            relation_type,
        )
        span = get_current_span()
        span.set_attribute("lithos.agent", agent)
        span.set_attribute("lithos.relation_type", relation_type)
        task_id = await server.coordination.spawn_task(
            source_task_id=source_task_id,
            title=title,
            agent=agent,
            description=description,
            relation_type=relation_type,
            inherit_project=inherit_project,
            inherit_tags=inherit_tags,
            inherit_context=inherit_context,
            metadata=metadata,
        )
        span.set_attribute("lithos.task_id", task_id)
        await server._emit(
            LithosEvent(
                type=TASK_CREATED,
                agent=agent,
                payload={"task_id": task_id, "title": title},
            )
        )
        return {"task_id": task_id}

    @mcp.tool()
    @tool_metrics()
    @tool_span(map_coordination_error=True)
    async def lithos_task_status(
        task_id: str,
    ) -> dict[str, list[dict[str, Any]]]:
        """Get the full record of a specific task with its active claims.

        Args:
            task_id: Task ID to look up

        Returns:
            Dict with tasks list containing id, title, description, status,
            created_by, created_at, resolved_at, tags, metadata, outcome,
            and claims. Returns an empty tasks list if the task does not
            exist (mirrors the historical behaviour).
        """
        logger.info("lithos_task_status task_id=%s", task_id)
        span = get_current_span()
        span.set_attribute("lithos.task_id", task_id)

        statuses = await server.coordination.get_task_status(task_id)

        return {
            "tasks": [
                {
                    **_serialize_task_record(s),
                    "claims": [
                        {
                            "agent": c.agent,
                            "aspect": c.aspect,
                            "expires_at": c.expires_at.isoformat(),
                        }
                        for c in s.claims
                    ],
                }
                for s in statuses
            ]
        }

    @mcp.tool()
    @tool_metrics()
    @tool_span(map_coordination_error=True)
    async def lithos_task_get(
        task_id: str,
    ) -> dict[str, Any]:
        """Get the full record of a single task by ID.

        Returns the task on its own (not wrapped in a list) so callers
        that already know the ID don't have to unwrap a one-element
        response. Does not include claims — use ``lithos_task_status``
        when you need claims alongside the task fields.

        Args:
            task_id: Task ID to look up

        Returns:
            Dict with task fields (id, title, description, status,
            created_by, created_at, resolved_at, tags, metadata, outcome).
            Returns the standard error envelope
            ``{status: "error", code: "task_not_found", message: ...}``
            when no task matches.
        """
        logger.info("lithos_task_get task_id=%s", task_id)
        span = get_current_span()
        span.set_attribute("lithos.task_id", task_id)

        task = await server.coordination.get_task(task_id)
        if task is None:
            return error_envelope("task_not_found", f"Task '{task_id}' not found.")

        return {"task": _serialize_task_record(task)}
