"""Corpus-note MCP tools: write, patch, delete."""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

from lithos.envelopes import error_envelope, invalid_input_envelope
from lithos.intake import DeleteRequest, NoteUpdateRequest, WriteRequest
from lithos.knowledge import (
    _UNSET,
    VALID_ACCESS_SCOPES,
    VALID_NOTE_TYPES,
    VALID_STATUSES,
    _UnsetType,
    validate_extra_metadata,
)
from lithos.telemetry import get_current_span, tool_metrics
from lithos.tools._seam import tool_span

if TYPE_CHECKING:
    from lithos.server import LithosServer

logger = logging.getLogger(__name__)


def register(mcp: FastMCP, server: LithosServer) -> None:
    """Register the note tools. See the late-binding rule in :mod:`lithos.tools`."""

    @mcp.tool()
    @tool_metrics()
    @tool_span()
    async def lithos_write(
        title: str,
        content: str,
        agent: str,
        tags: list[str] | None = None,
        confidence: float | None = None,
        path: str | None = None,
        id: str | None = None,
        source_task: str | None = None,
        source_url: str | None = None,
        derived_from_ids: list[str] | None = None,
        ttl_hours: float | None = None,
        expires_at: str | None = None,
        expected_version: int | None = None,
        schema_version: int | None = None,
        namespace: str | None = None,
        access_scope: str | None = None,
        note_type: str | None = None,
        status: str | None = None,
        summaries: dict | None = None,
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        """Create or update a knowledge file.

        Args are grouped below by role. The grouping is documentation only —
        all parameters remain flat at the MCP boundary. See
        `docs/plans/unified-write-contract.md` for the normative field
        contract.

        Args:
            title: Title of the knowledge item.
            content: Markdown content (without frontmatter).
            agent: Your agent identifier.

            --- Identity & metadata ---
            id: UUID to update existing; omit to create new.
            tags: List of tags. On update: null/omit preserves existing; [] clears
                all tags; non-empty list replaces.
            metadata: Free-form key/value dict persisted into the document's
                frontmatter. On update: null/omit preserves existing; {} clears
                all metadata; a non-empty dict is an additive per-key merge (a
                key whose value is null deletes it). Keys must be strings and
                must not collide with reserved frontmatter fields (e.g. title,
                tags, version) — such writes are rejected with code "invalid_input".
            confidence: Confidence score 0-1 (default: 1.0 on create). On update:
                null/omit preserves existing; float sets new value. Integers
                are coerced to float; anything else that is not a finite
                number in [0.0, 1.0] (non-numeric, bool, NaN/inf, or
                out-of-range) is rejected with code "invalid_input".
            path: Where to store the note. Two accepted forms:
                - Subdirectory (e.g., "procedures") — the filename is derived
                  from the title (slugified) and ".md" is appended.
                - Full relative file path ending in ".md"
                  (e.g., "procedures/my-doc.md") — the final segment is used
                  as the filename verbatim; the title does NOT influence the
                  filename in this mode.
                Intermediate path segments may not end in ".md"; such inputs
                are rejected with code "invalid_input" to prevent
                accidental creation of directories whose names end in ".md".

            --- Provenance ---
            source_url: URL provenance for this knowledge. On update: null/omit
                preserves existing; "" clears; string sets new value.
            derived_from_ids: List of source document UUIDs this note was derived
                from. On update: null/omit preserves existing; [] clears;
                non-empty list replaces.
            source_task: Task ID this knowledge came from.

            --- Freshness ---
            ttl_hours: Time-to-live in hours from now. Computes expires_at.
                Mutually exclusive with expires_at.
            expires_at: Absolute ISO 8601 expiry datetime. On update: null/omit
                preserves existing; "" clears; ISO string sets new value.
                Mutually exclusive with ttl_hours.

            --- Concurrency ---
            expected_version: If provided on update, reject with version_conflict if the
                document's current version differs. Omit to skip version checking.
                On create, this parameter is silently ignored.

            --- LCMA ---
            schema_version: LCMA schema version (default 1 on create).
            namespace: LCMA namespace. Persisted only if explicitly passed;
                derived at read time otherwise.
            access_scope: shared|task|agent_private (default shared on create).
                task requires source_task.
            note_type: observation|agent_finding|summary|concept|task_record|hypothesis
                (default observation on create).
            status: active|archived|quarantined (default active on create).
            summaries: Optional dict with short/long summary strings.

        Returns:
            On success: {"status": "created"|"updated", "id", "path", "version",
            "warnings"}. Actionable outcomes keep their own top-level status:
            duplicate / slug_collision / path_collision / version_conflict
            (version_conflict carries "current_version"). All other failures
            use the standard error envelope {"status": "error", "code",
            "message"} — validation failures carry code "invalid_input".
        """
        logger.info("lithos_write agent=%s title=%r update=%s", agent, title, id is not None)
        span = get_current_span()
        span.set_attribute("lithos.agent", agent)
        span.set_attribute("lithos.is_update", id is not None)

        # Validate ttl_hours / expires_at mutual exclusion
        if ttl_hours is not None and expires_at is not None:
            return invalid_input_envelope("Provide either ttl_hours or expires_at, not both.")

        # Validate ttl_hours
        if ttl_hours is not None and (
            not isinstance(ttl_hours, (int, float))
            or math.isnan(ttl_hours)
            or math.isinf(ttl_hours)
            or ttl_hours <= 0
        ):
            return invalid_input_envelope("ttl_hours must be a finite positive number.")

        # Validate LCMA enum fields
        if access_scope is not None and access_scope not in VALID_ACCESS_SCOPES:
            return invalid_input_envelope(
                f"Invalid access_scope: {access_scope!r}. "
                f"Must be one of {sorted(VALID_ACCESS_SCOPES)}"
            )
        if note_type is not None and note_type not in VALID_NOTE_TYPES:
            return invalid_input_envelope(
                f"Invalid note_type: {note_type!r}. Must be one of {sorted(VALID_NOTE_TYPES)}"
            )
        if status is not None and status not in VALID_STATUSES:
            return invalid_input_envelope(
                f"Invalid status: {status!r}. Must be one of {sorted(VALID_STATUSES)}"
            )

        # Validate summaries shape
        if summaries is not None:
            if not isinstance(summaries, dict):
                return invalid_input_envelope(
                    "summaries must be an object with 'short' and/or 'long' string fields."
                )
            unknown_keys = set(summaries.keys()) - {"short", "long"}
            if unknown_keys:
                return invalid_input_envelope(
                    f"summaries has unknown keys: {sorted(unknown_keys)}. "
                    f"Allowed keys: ['long', 'short']."
                )
            for k, v in summaries.items():
                if not isinstance(v, str):
                    return invalid_input_envelope(
                        f"summaries.{k} must be a string, got {type(v).__name__}."
                    )

        # Validate metadata shape at the boundary for a fast, clean
        # envelope. The same rule is enforced in the storage layer
        # (KnowledgeManager) so the invariant holds for every caller.
        if metadata is not None:
            try:
                validate_extra_metadata(metadata)
            except ValueError as e:
                return invalid_input_envelope(str(e))

        # Validate task-scope create-time invariant. The update-time
        # case is enforced under ``_write_lock`` inside
        # ``KnowledgeManager.update`` to avoid the TOCTOU window
        # described in ADR-0003.
        if access_scope == "task" and id is None and not source_task:
            return invalid_input_envelope("access_scope='task' requires source_task")

        # Emit freshness span attributes
        if ttl_hours is not None:
            span.set_attribute("freshness.ttl_hours", ttl_hours)
        elif expires_at is not None and expires_at != "":
            span.set_attribute("freshness.expires_at_set", True)

        # Compute expires_at_dt from ttl_hours or expires_at string
        expires_at_dt: datetime | None | _UnsetType
        if ttl_hours is not None:
            expires_at_dt = datetime.now(UTC) + timedelta(hours=ttl_hours)
        elif id is not None:
            # Update path: map MCP boundary to manager semantics
            # None (omitted) → _UNSET (preserve), "" → None (clear), str → parse
            if expires_at is None:
                expires_at_dt = _UNSET
            elif expires_at == "":
                expires_at_dt = None
            else:
                try:
                    expires_at_dt = datetime.fromisoformat(expires_at)
                    if expires_at_dt.tzinfo is None:
                        expires_at_dt = expires_at_dt.replace(tzinfo=UTC)
                    else:
                        expires_at_dt = expires_at_dt.astimezone(UTC)
                except ValueError:
                    return invalid_input_envelope(f"Invalid expires_at datetime: {expires_at}")
        else:
            # Create path: None means no expiry, str → parse
            if expires_at is None:
                expires_at_dt = None
            else:
                try:
                    expires_at_dt = datetime.fromisoformat(expires_at)
                    if expires_at_dt.tzinfo is None:
                        expires_at_dt = expires_at_dt.replace(tzinfo=UTC)
                    else:
                        expires_at_dt = expires_at_dt.astimezone(UTC)
                except ValueError:
                    return invalid_input_envelope(f"Invalid expires_at datetime: {expires_at}")

        # Translate MCP wire shape into intake field semantics:
        #   None  (omitted) → _UNSET (preserve)
        #   ""               → None (clear)
        #   value            → set
        # ``expires_at_dt`` already encodes this for the freshness
        # field above; we mirror the same rule for the rest here.
        if id is not None:
            url_arg: str | None | _UnsetType
            if source_url is None:
                url_arg = _UNSET
            elif source_url == "":
                url_arg = None
            else:
                url_arg = source_url
            prov_arg: list[str] | None | _UnsetType = (
                _UNSET if derived_from_ids is None else derived_from_ids
            )
            tags_arg: list[str] | _UnsetType = _UNSET if tags is None else tags
            conf_arg: float | _UnsetType = _UNSET if confidence is None else confidence
            source_arg: str | None | _UnsetType = _UNSET if source_task is None else source_task
            sv_arg: int | _UnsetType = _UNSET if schema_version is None else schema_version
            ns_arg: str | None | _UnsetType = _UNSET if namespace is None else namespace
            as_arg: str | None | _UnsetType = _UNSET if access_scope is None else access_scope
            nt_arg: str | None | _UnsetType = _UNSET if note_type is None else note_type
            st_arg: str | None | _UnsetType = _UNSET if status is None else status
            sum_arg: dict | None | _UnsetType = _UNSET if summaries is None else summaries
            meta_arg: dict | _UnsetType = _UNSET if metadata is None else metadata
        else:
            # Create path forwards raw values; KnowledgeManager.create
            # applies its own defaults.
            url_arg = source_url or None
            prov_arg = derived_from_ids
            tags_arg = tags  # type: ignore[assignment]
            conf_arg = confidence  # type: ignore[assignment]
            source_arg = source_task
            sv_arg = schema_version  # type: ignore[assignment]
            ns_arg = namespace
            as_arg = access_scope
            nt_arg = note_type
            st_arg = status
            sum_arg = summaries
            meta_arg = metadata if metadata is not None else _UNSET

        request = WriteRequest(
            title=title,
            content=content,
            id=id,
            tags=tags_arg,
            confidence=conf_arg,
            path=path,
            source_task=source_arg,
            source_url=url_arg,
            derived_from_ids=prov_arg,
            expires_at=expires_at_dt,
            expected_version=expected_version,
            schema_version=sv_arg,
            namespace=ns_arg,
            access_scope=as_arg,
            note_type=nt_arg,
            lcma_status=st_arg,
            summaries=sum_arg,
            metadata=meta_arg,
        )

        outcome = await server.intake.write(agent, request)

        if outcome.status == "slug_collision":
            span.set_attribute("lithos.write_status", "slug_collision")
            return {
                "status": "slug_collision",
                "message": outcome.message,
                "existing_id": outcome.slug_collision_existing_id,
                "warnings": [],
            }
        if outcome.status == "path_collision":
            span.set_attribute("lithos.write_status", "path_collision")
            return {
                "status": "path_collision",
                "message": outcome.message,
                "existing_id": outcome.path_collision_existing_id,
                "warnings": list(outcome.warnings),
            }
        if outcome.status == "duplicate":
            span.set_attribute("lithos.write_status", "duplicate")
            dup = outcome.duplicate_of
            return {
                "status": "duplicate",
                "duplicate_of": {
                    "id": dup.id,
                    "title": dup.title,
                    "source_url": dup.source_url,
                }
                if dup
                else None,
                "message": outcome.message,
                "warnings": list(outcome.warnings),
            }
        if outcome.status == "version_conflict":
            # An actionable write outcome, not an error: read-merge-write
            # retry loops branch on this status (see CHANGELOG).
            span.set_attribute("lithos.write_status", "version_conflict")
            conflict: dict[str, Any] = {
                "status": "version_conflict",
                "message": outcome.message,
                "warnings": list(outcome.warnings),
            }
            if outcome.current_version is not None:
                conflict["current_version"] = outcome.current_version
            return conflict
        if outcome.status in ("invalid_input", "content_too_large", "error"):
            span.set_attribute("lithos.write_status", outcome.status)
            if outcome.status == "invalid_input":
                return invalid_input_envelope(outcome.message or "")
            code = "internal_error" if outcome.status == "error" else outcome.status
            return error_envelope(code, outcome.message or "")

        doc = outcome.document
        assert doc is not None
        span.set_attribute("lithos.doc_id", doc.id)
        span.set_attribute("lithos.write_status", outcome.status)
        span.set_attribute(
            "lithos.provenance.source_count",
            len(doc.metadata.derived_from_ids),
        )
        if outcome.warnings:
            span.set_attribute("lithos.provenance.warning_count", len(outcome.warnings))
        logger.info("lithos_write completed doc_id=%s status=%s", doc.id, outcome.status)
        return {
            "status": outcome.status,
            "id": doc.id,
            "path": str(doc.path),
            "version": doc.metadata.version,
            "warnings": list(outcome.warnings),
        }

    @mcp.tool()
    @tool_metrics()
    @tool_span()
    async def lithos_note_update(
        id: str,
        agent: str,
        title: str | None = None,
        tags: list[str] | None = None,
        status: str | None = None,
        metadata: dict | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """Patch a note's frontmatter (tags/metadata/title/status) without resending its body.

        The note counterpart to ``lithos_task_update``: a per-field patch
        that leaves the markdown body untouched. Use this instead of
        ``lithos_write`` whenever you only need to change frontmatter — it
        removes the read → reconstruct-body → write round-trip (and the
        lost-update risk that comes with reproducing the body), since the
        body is never read into the request at all.

        At least one of ``title``, ``tags``, ``status``, or ``metadata`` must
        be provided.

        Args:
            id: UUID of the note to patch.
            agent: Your agent identifier.
            title: New title. null/omit preserves existing. Renaming may
                change the note's slug; a collision with another note's
                slug is rejected as ``slug_collision``.
            tags: null/omit preserves existing; ``[]`` clears all tags; a
                non-empty list replaces them.
            status: active|archived|quarantined. null/omit preserves existing.
            metadata: Additive per-key merge into the note's existing
                frontmatter metadata. A key whose value is null deletes it;
                other keys are set; keys not mentioned are preserved. As with
                ``lithos_task_update`` there is no wholesale-clear affordance —
                ``metadata={}`` makes no metadata change (and, with no other
                field set, is rejected with code "invalid_input"). Keys must be
                strings and must not collide with reserved frontmatter fields
                (e.g. title, tags, version) — such patches are rejected with
                code "invalid_input".
            expected_version: If provided, reject with version_conflict when
                the note's current version differs. Omit to skip the check.

        Returns:
            On success: {"status": "updated", ...}. Actionable outcomes keep
            their own top-level status: slug_collision / duplicate /
            version_conflict (the latter carries "current_version"). All other
            failures use the standard error envelope {"status": "error",
            "code", "message"} — validation failures carry code
            "invalid_input"; an unknown id carries code "note_not_found".
        """
        # An empty metadata dict carries no change (it maps to _UNSET below),
        # so it does not count as a provided field — `metadata={}` alone is
        # rejected rather than producing a version-bumping, event-emitting no-op.
        has_metadata_change = metadata is not None and metadata != {}
        if title is None and tags is None and status is None and not has_metadata_change:
            return invalid_input_envelope(
                "At least one of title, tags, status, or metadata must be provided "
                "(an empty metadata dict makes no change)."
            )

        logger.info("lithos_note_update agent=%s id=%s", agent, id)
        span = get_current_span()
        span.set_attribute("lithos.agent", agent)
        span.set_attribute("lithos.doc_id", id)

        # Validate status enum at the boundary for a fast, clean envelope.
        if status is not None and status not in VALID_STATUSES:
            return invalid_input_envelope(
                f"Invalid status: {status!r}. Must be one of {sorted(VALID_STATUSES)}"
            )

        # Validate metadata shape at the boundary. The same rule is
        # enforced in KnowledgeManager so the invariant holds for every
        # caller; checking here gives a fast, clean envelope.
        if metadata is not None:
            try:
                validate_extra_metadata(metadata)
            except ValueError as e:
                return invalid_input_envelope(str(e))

        # Translate the MCP wire shape into KnowledgeManager semantics:
        #   None (omitted) → _UNSET (preserve)
        #   tags=[]        → clear all tags
        #   metadata={}    → _UNSET (no-op, mirroring lithos_task_update)
        tags_arg: list[str] | _UnsetType = _UNSET if tags is None else tags
        st_arg: str | None | _UnsetType = _UNSET if status is None else status
        meta_arg: dict | _UnsetType = _UNSET if metadata is None or metadata == {} else metadata

        request = NoteUpdateRequest(
            id=id,
            title=title,
            tags=tags_arg,
            lcma_status=st_arg,
            metadata=meta_arg,
            expected_version=expected_version,
        )

        try:
            outcome = await server.intake.note_update(agent, request)
        except FileNotFoundError:
            span.set_attribute("lithos.write_status", "note_not_found")
            return error_envelope("note_not_found", f"Note {id} not found")

        if outcome.status == "slug_collision":
            span.set_attribute("lithos.write_status", "slug_collision")
            return {
                "status": "slug_collision",
                "message": outcome.message,
                "existing_id": outcome.slug_collision_existing_id,
                "warnings": [],
            }
        if outcome.status == "duplicate":
            span.set_attribute("lithos.write_status", "duplicate")
            dup = outcome.duplicate_of
            return {
                "status": "duplicate",
                "duplicate_of": {
                    "id": dup.id,
                    "title": dup.title,
                    "source_url": dup.source_url,
                }
                if dup
                else None,
                "message": outcome.message,
                "warnings": list(outcome.warnings),
            }
        if outcome.status == "version_conflict":
            # An actionable write outcome, not an error: read-merge-write
            # retry loops branch on this status (see CHANGELOG).
            span.set_attribute("lithos.write_status", "version_conflict")
            conflict: dict[str, Any] = {
                "status": "version_conflict",
                "message": outcome.message,
                "warnings": list(outcome.warnings),
            }
            if outcome.current_version is not None:
                conflict["current_version"] = outcome.current_version
            return conflict
        if outcome.status in ("invalid_input", "error"):
            span.set_attribute("lithos.write_status", outcome.status)
            if outcome.status == "invalid_input":
                return invalid_input_envelope(outcome.message or "")
            return error_envelope("internal_error", outcome.message or "")

        doc = outcome.document
        assert doc is not None
        span.set_attribute("lithos.write_status", outcome.status)
        logger.info("lithos_note_update completed doc_id=%s status=%s", doc.id, outcome.status)
        return {
            "status": outcome.status,
            "id": doc.id,
            "path": str(doc.path),
            "version": doc.metadata.version,
            "warnings": list(outcome.warnings),
        }

    @mcp.tool()
    @tool_metrics()
    @tool_span()
    async def lithos_delete(
        id: str,
        agent: str,
    ) -> dict[str, Any]:
        """Delete a knowledge file.

        Args:
            id: UUID of knowledge item to delete
            agent: Agent performing deletion (required for audit trail)

        Returns:
            Dict with success boolean, or error envelope if document not found
        """
        logger.info("lithos_delete id=%s agent=%s", id, agent)
        span = get_current_span()
        span.set_attribute("lithos.id", id)
        span.set_attribute("lithos.agent", agent)

        outcome = await server.intake.delete(agent, DeleteRequest(id=id))
        if outcome.status == "not_found":
            return error_envelope("doc_not_found", f"Document not found: {id}")
        return {"success": True}
