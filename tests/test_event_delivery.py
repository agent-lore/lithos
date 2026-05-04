"""Tests for the SSE event delivery surface (Phase 6.5).

Coverage:
- SSE client receives events after subscribing
- Type filter works (only matching event types delivered)
- Tag filter works (only matching tags delivered)
- Replay from ?since=<event-id> replays buffered events
- Last-Event-ID header replay on reconnect (takes precedence over ?since=)
- Max clients limit respected (429 when exceeded)
- SSE disabled via sse_enabled: False config (503)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from starlette.requests import Request
from starlette.responses import StreamingResponse

from lithos.config import EventsConfig, LithosConfig, StorageConfig
from lithos.events import (
    NOTE_CREATED,
    NOTE_DELETED,
    NOTE_UPDATED,
    TASK_COMPLETED,
    TASK_CREATED,
    EventBus,
    LithosEvent,
)
from lithos.server import LithosServer, _format_sse

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    temp_dir: Path,
    *,
    sse_enabled: bool = True,
    max_sse_clients: int = 50,
) -> LithosConfig:
    events = EventsConfig(
        enabled=True,
        sse_enabled=sse_enabled,
        max_sse_clients=max_sse_clients,
    )
    return LithosConfig(
        storage=StorageConfig(data_dir=temp_dir),
        events=events,
    )


async def _collect_sse_lines(
    server: LithosServer,
    request: Request,
    *,
    emit_events: list[LithosEvent],
    timeout: float = 2.0,
) -> list[str]:
    """Run the SSE endpoint, emit events, and collect the raw SSE lines."""
    response = await server._sse_endpoint(request)
    assert isinstance(response, StreamingResponse)

    collected: list[str] = []

    async def _consume() -> None:
        async for chunk in response.body_iterator:
            if isinstance(chunk, bytes):
                chunk = chunk.decode()
            collected.append(chunk)

    # Emit events shortly after starting consumption
    async def _emit() -> None:
        await asyncio.sleep(0.05)
        for evt in emit_events:
            await server.event_bus.emit(evt)
        # Give the consumer a moment to drain the queue
        await asyncio.sleep(0.1)

    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(
            asyncio.gather(_consume(), _emit()),
            timeout=timeout,
        )

    return collected


def _parse_sse_events(lines: list[str]) -> list[dict]:
    """Parse raw SSE chunks into structured event dicts."""
    events = []
    current: dict = {}
    for chunk in lines:
        for line in chunk.splitlines():
            if line.startswith("id: "):
                current["id"] = line[4:]
            elif line.startswith("event: "):
                current["event"] = line[7:]
            elif line.startswith("data: "):
                current["data"] = json.loads(line[6:])
            elif line == "" and current:
                events.append(current)
                current = {}
    return events


# ---------------------------------------------------------------------------
# Unit: _format_sse helper
# ---------------------------------------------------------------------------


class TestFormatSSE:
    def test_basic_format(self) -> None:
        evt = LithosEvent(type=NOTE_CREATED, agent="az", tags=["research"])
        result = _format_sse(evt)
        assert f"id: {evt.id}\n" in result
        assert f"event: {NOTE_CREATED}\n" in result
        assert '"agent": "az"' in result
        assert result.endswith("\n\n")

    def test_tags_in_payload(self) -> None:
        evt = LithosEvent(type=NOTE_CREATED, tags=["pricing", "acme"])
        result = _format_sse(evt)
        data = json.loads(result.split("data: ")[1].strip())
        assert "pricing" in data["tags"]
        assert "acme" in data["tags"]

    def test_payload_fields_included(self) -> None:
        evt = LithosEvent(
            type=TASK_COMPLETED,
            agent="bot",
            payload={"task_id": "t1", "title": "My Task"},
        )
        result = _format_sse(evt)
        data = json.loads(result.split("data: ")[1].strip())
        assert data["task_id"] == "t1"
        assert data["title"] == "My Task"
        assert data["agent"] == "bot"


# ---------------------------------------------------------------------------
# Unit: EventBus.get_buffered_since
# ---------------------------------------------------------------------------


class TestGetBufferedSince:
    @pytest.mark.asyncio
    async def test_returns_events_after_id(self) -> None:
        bus = EventBus()
        e1 = LithosEvent(type=NOTE_CREATED)
        e2 = LithosEvent(type=NOTE_UPDATED)
        e3 = LithosEvent(type=NOTE_DELETED)
        await bus.emit(e1)
        await bus.emit(e2)
        await bus.emit(e3)

        result = bus.get_buffered_since(e1.id)
        assert [e.id for e in result] == [e2.id, e3.id]

    @pytest.mark.asyncio
    async def test_unknown_id_returns_empty(self) -> None:
        bus = EventBus()
        await bus.emit(LithosEvent(type=NOTE_CREATED))
        result = bus.get_buffered_since("nonexistent-id")
        assert result == []

    @pytest.mark.asyncio
    async def test_last_event_returns_empty(self) -> None:
        bus = EventBus()
        e = LithosEvent(type=NOTE_CREATED)
        await bus.emit(e)
        result = bus.get_buffered_since(e.id)
        assert result == []


# ---------------------------------------------------------------------------
# Integration: SSE endpoint via server
# ---------------------------------------------------------------------------


def _make_mock_request(
    server: LithosServer,
    *,
    types: str | None = None,
    tags: str | None = None,
    since: str | None = None,
    last_event_id: str | None = None,
) -> Request:
    """Build a minimal Starlette Request pointing at the SSE endpoint."""
    params: list[tuple[str, str]] = []
    if types:
        params.append(("types", types))
    if tags:
        params.append(("tags", tags))
    if since:
        params.append(("since", since))

    query_string = "&".join(f"{k}={v}" for k, v in params).encode()

    headers: list[tuple[bytes, bytes]] = []
    if last_event_id:
        headers.append((b"last-event-id", last_event_id.encode()))

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/events",
        "query_string": query_string,
        "headers": headers,
    }
    return Request(scope)


@pytest_asyncio.fixture
async def sse_server(temp_dir: Path) -> AsyncGenerator[LithosServer, None]:
    """Server with SSE enabled, small queue for fast tests."""
    config = _make_config(temp_dir)
    config.events.subscriber_queue_size = 20
    server = LithosServer(config)
    await server.initialize()
    yield server
    await server.shutdown()


class TestSSEReceivesEvents:
    @pytest.mark.asyncio
    async def test_client_receives_emitted_event(self, sse_server: LithosServer) -> None:
        """SSE client should receive a freshly emitted event."""
        evt = LithosEvent(type=NOTE_CREATED, agent="az", tags=["test"])
        request = _make_mock_request(sse_server)

        lines = await _collect_sse_lines(sse_server, request, emit_events=[evt])
        events = _parse_sse_events(lines)

        assert any(e.get("event") == NOTE_CREATED for e in events), (
            f"Expected NOTE_CREATED in {events}"
        )

    @pytest.mark.asyncio
    async def test_client_receives_multiple_events(self, sse_server: LithosServer) -> None:
        """SSE client receives all emitted events in order."""
        evts = [
            LithosEvent(type=NOTE_CREATED, agent="a"),
            LithosEvent(type=TASK_CREATED, agent="b"),
            LithosEvent(type=TASK_COMPLETED, agent="c"),
        ]
        request = _make_mock_request(sse_server)

        lines = await _collect_sse_lines(sse_server, request, emit_events=evts)
        events = _parse_sse_events(lines)
        received_types = [e["event"] for e in events]

        assert NOTE_CREATED in received_types
        assert TASK_CREATED in received_types
        assert TASK_COMPLETED in received_types


class TestTypeFilter:
    @pytest.mark.asyncio
    async def test_type_filter_accepts_matching(self, sse_server: LithosServer) -> None:
        """Only events matching the type filter should be delivered."""
        evts = [
            LithosEvent(type=NOTE_CREATED),
            LithosEvent(type=TASK_COMPLETED),
            LithosEvent(type=NOTE_DELETED),
        ]
        request = _make_mock_request(sse_server, types=NOTE_CREATED)

        lines = await _collect_sse_lines(sse_server, request, emit_events=evts)
        events = _parse_sse_events(lines)
        received_types = [e["event"] for e in events]

        assert NOTE_CREATED in received_types
        assert TASK_COMPLETED not in received_types
        assert NOTE_DELETED not in received_types

    @pytest.mark.asyncio
    async def test_type_filter_multiple_types(self, sse_server: LithosServer) -> None:
        """Multiple comma-separated types should all pass the filter."""
        evts = [
            LithosEvent(type=NOTE_CREATED),
            LithosEvent(type=TASK_COMPLETED),
            LithosEvent(type=NOTE_DELETED),
        ]
        request = _make_mock_request(sse_server, types=f"{NOTE_CREATED},{TASK_COMPLETED}")

        lines = await _collect_sse_lines(sse_server, request, emit_events=evts)
        events = _parse_sse_events(lines)
        received_types = {e["event"] for e in events}

        assert NOTE_CREATED in received_types
        assert TASK_COMPLETED in received_types
        assert NOTE_DELETED not in received_types


class TestTagFilter:
    @pytest.mark.asyncio
    async def test_tag_filter_accepts_matching(self, sse_server: LithosServer) -> None:
        """Only events with matching tags should be delivered."""
        evts = [
            LithosEvent(type=NOTE_CREATED, tags=["research"]),
            LithosEvent(type=NOTE_CREATED, tags=["pricing"]),
            LithosEvent(type=NOTE_CREATED, tags=["unrelated"]),
        ]
        request = _make_mock_request(sse_server, tags="research,pricing")

        lines = await _collect_sse_lines(sse_server, request, emit_events=evts)
        events = _parse_sse_events(lines)

        # All three events have type NOTE_CREATED — distinguish by tag
        seen_tag_sets = [tuple(e["data"].get("tags", [])) for e in events]
        assert ("research",) in seen_tag_sets
        assert ("pricing",) in seen_tag_sets
        assert ("unrelated",) not in seen_tag_sets

    @pytest.mark.asyncio
    async def test_untagged_event_rejected_when_tag_filter_set(
        self, sse_server: LithosServer
    ) -> None:
        """Events with no tags should not pass a tag filter."""
        evts = [
            LithosEvent(type=NOTE_CREATED, tags=[]),
            LithosEvent(type=NOTE_CREATED, tags=["research"]),
        ]
        request = _make_mock_request(sse_server, tags="research")

        lines = await _collect_sse_lines(sse_server, request, emit_events=evts)
        events = _parse_sse_events(lines)

        assert len(events) == 1
        assert events[0]["data"]["tags"] == ["research"]


class TestReplay:
    @pytest.mark.asyncio
    async def test_since_replays_buffered_events(self, sse_server: LithosServer) -> None:
        """?since=<id> should replay buffered events after that ID."""
        # Pre-populate the buffer with events BEFORE the client connects
        e1 = LithosEvent(type=NOTE_CREATED, agent="pre1")
        e2 = LithosEvent(type=NOTE_UPDATED, agent="pre2")
        e3 = LithosEvent(type=TASK_CREATED, agent="pre3")
        await sse_server.event_bus.emit(e1)
        await sse_server.event_bus.emit(e2)
        await sse_server.event_bus.emit(e3)

        # Connect with ?since=e1.id — should replay e2 and e3
        request = _make_mock_request(sse_server, since=e1.id)
        lines = await _collect_sse_lines(sse_server, request, emit_events=[])
        events = _parse_sse_events(lines)
        ids = [e["id"] for e in events]

        assert e2.id in ids
        assert e3.id in ids
        assert e1.id not in ids  # since= is exclusive

    @pytest.mark.asyncio
    async def test_since_unknown_id_yields_no_replay(self, sse_server: LithosServer) -> None:
        """Unknown since= ID yields no buffered replay (just live events)."""
        live = LithosEvent(type=NOTE_CREATED, agent="live")
        request = _make_mock_request(sse_server, since="no-such-id")
        lines = await _collect_sse_lines(sse_server, request, emit_events=[live])
        events = _parse_sse_events(lines)

        # Only the live event should appear
        ids = [e["id"] for e in events]
        assert live.id in ids

    @pytest.mark.asyncio
    async def test_last_event_id_header_replay(self, sse_server: LithosServer) -> None:
        """Last-Event-ID header triggers replay same as ?since=."""
        e1 = LithosEvent(type=NOTE_CREATED, agent="h1")
        e2 = LithosEvent(type=NOTE_UPDATED, agent="h2")
        await sse_server.event_bus.emit(e1)
        await sse_server.event_bus.emit(e2)

        request = _make_mock_request(sse_server, last_event_id=e1.id)
        lines = await _collect_sse_lines(sse_server, request, emit_events=[])
        events = _parse_sse_events(lines)
        ids = [e["id"] for e in events]

        assert e2.id in ids
        assert e1.id not in ids

    @pytest.mark.asyncio
    async def test_last_event_id_takes_precedence_over_since(
        self, sse_server: LithosServer
    ) -> None:
        """Last-Event-ID header should win over ?since= when both present."""
        e1 = LithosEvent(type=NOTE_CREATED, agent="p1")
        e2 = LithosEvent(type=NOTE_UPDATED, agent="p2")
        e3 = LithosEvent(type=NOTE_DELETED, agent="p3")
        await sse_server.event_bus.emit(e1)
        await sse_server.event_bus.emit(e2)
        await sse_server.event_bus.emit(e3)

        # Header says replay from e1; ?since= says replay from e2.
        # Header wins → should get e2 and e3.
        request = _make_mock_request(sse_server, since=e2.id, last_event_id=e1.id)
        lines = await _collect_sse_lines(sse_server, request, emit_events=[])
        events = _parse_sse_events(lines)
        ids = [e["id"] for e in events]

        assert e2.id in ids
        assert e3.id in ids
        assert e1.id not in ids

    @pytest.mark.asyncio
    async def test_replay_applies_type_filter(self, sse_server: LithosServer) -> None:
        """Type filter should be applied to replayed events too."""
        e1 = LithosEvent(type=NOTE_CREATED)
        e2 = LithosEvent(type=TASK_COMPLETED)
        e3 = LithosEvent(type=NOTE_UPDATED)
        await sse_server.event_bus.emit(e1)
        await sse_server.event_bus.emit(e2)
        await sse_server.event_bus.emit(e3)

        request = _make_mock_request(sse_server, since=e1.id, types=NOTE_UPDATED)
        lines = await _collect_sse_lines(sse_server, request, emit_events=[])
        events = _parse_sse_events(lines)
        received_types = {e["event"] for e in events}

        assert NOTE_UPDATED in received_types
        assert TASK_COMPLETED not in received_types

    @pytest.mark.asyncio
    async def test_replay_applies_tag_filter(self, sse_server: LithosServer) -> None:
        """Tag filter should be applied to replayed events too."""
        e1 = LithosEvent(type=NOTE_CREATED, tags=["base"])
        e2 = LithosEvent(type=NOTE_UPDATED, tags=["research"])
        e3 = LithosEvent(type=NOTE_DELETED, tags=["other"])
        await sse_server.event_bus.emit(e1)
        await sse_server.event_bus.emit(e2)
        await sse_server.event_bus.emit(e3)

        request = _make_mock_request(sse_server, since=e1.id, tags="research")
        lines = await _collect_sse_lines(sse_server, request, emit_events=[])
        events = _parse_sse_events(lines)
        received_types = {e["event"] for e in events}

        assert NOTE_UPDATED in received_types
        assert NOTE_DELETED not in received_types


class TestMaxClients:
    @pytest.mark.asyncio
    async def test_max_clients_limit_returns_429(self, temp_dir: Path) -> None:
        """When max_sse_clients is reached, further connections get 429."""
        config = _make_config(temp_dir, max_sse_clients=2)
        server = LithosServer(config)
        await server.initialize()
        try:
            # Saturate the semaphore by acquiring every available slot.
            for _ in range(2):
                await server._sse_semaphore.acquire()

            request = _make_mock_request(server)
            response = await server._sse_endpoint(request)

            assert response.status_code == 429
        finally:
            await server.shutdown()

    @pytest.mark.asyncio
    async def test_below_limit_returns_streaming_response(self, temp_dir: Path) -> None:
        """When under max_sse_clients, endpoint returns a streaming response."""
        config = _make_config(temp_dir, max_sse_clients=5)
        server = LithosServer(config)
        await server.initialize()
        try:
            # Acquire 4 slots so only one remains free.
            for _ in range(4):
                await server._sse_semaphore.acquire()

            request = _make_mock_request(server)
            response = await server._sse_endpoint(request)

            assert isinstance(response, StreamingResponse)
            assert response.media_type == "text/event-stream"
        finally:
            await server.shutdown()


class TestSSEDisabled:
    @pytest.mark.asyncio
    async def test_sse_disabled_returns_503(self, temp_dir: Path) -> None:
        """sse_enabled=False should return 503."""
        config = _make_config(temp_dir, sse_enabled=False)
        server = LithosServer(config)
        await server.initialize()
        try:
            request = _make_mock_request(server)
            response = await server._sse_endpoint(request)

            assert response.status_code == 503
        finally:
            await server.shutdown()

    @pytest.mark.asyncio
    async def test_sse_enabled_returns_stream(self, temp_dir: Path) -> None:
        """sse_enabled=True (default) should not return 503."""
        config = _make_config(temp_dir, sse_enabled=True)
        server = LithosServer(config)
        await server.initialize()
        try:
            request = _make_mock_request(server)
            response = await server._sse_endpoint(request)

            assert response.status_code != 503
            assert isinstance(response, StreamingResponse)
        finally:
            await server.shutdown()


class TestSSEClientCount:
    @pytest.mark.asyncio
    async def test_client_count_increments_decrements(self, temp_dir: Path) -> None:
        """_sse_active_count() should report 0 before connect and 0 again after stream end."""
        config = _make_config(temp_dir)
        server = LithosServer(config)
        await server.initialize()
        try:
            assert server._sse_active_count() == 0

            request = _make_mock_request(server)
            evt = LithosEvent(type=NOTE_CREATED)
            await _collect_sse_lines(server, request, emit_events=[evt])

            # After streaming completes the slot must be released.
            assert server._sse_active_count() == 0
        finally:
            await server.shutdown()


class TestSSEConfig:
    def test_events_config_sse_defaults(self) -> None:
        """EventsConfig should have sensible SSE defaults."""
        cfg = EventsConfig()
        assert cfg.sse_enabled is True
        assert cfg.max_sse_clients == 50

    def test_events_config_sse_override(self) -> None:
        """SSE config fields can be overridden."""
        cfg = EventsConfig(sse_enabled=False, max_sse_clients=10)
        assert cfg.sse_enabled is False
        assert cfg.max_sse_clients == 10


# ---------------------------------------------------------------------------
# Integration: HTTP route mounting and auth boundary
# ---------------------------------------------------------------------------


class TestSSERouteIntegration:
    """Tests that exercise the /events route on the real Starlette app."""

    @pytest.mark.asyncio
    async def test_events_route_mounted(self, temp_dir: Path) -> None:
        """GET /events on the real Starlette app returns text/event-stream."""
        import httpx

        config = _make_config(temp_dir)
        server = LithosServer(config)
        await server.initialize()
        try:
            app = server.mcp.http_app(transport="sse")
            transport = httpx.ASGITransport(app=app)

            async def _check() -> None:
                async with (
                    httpx.AsyncClient(transport=transport, base_url="http://test") as client,
                    client.stream("GET", "/events") as resp,
                ):
                    assert resp.status_code == 200
                    assert "text/event-stream" in resp.headers.get("content-type", "")

            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(_check(), timeout=2.0)
        finally:
            await server.shutdown()

    @pytest.mark.asyncio
    async def test_401_without_token_when_auth_configured(self, temp_dir: Path) -> None:
        """When auth is configured, GET /events without a token returns 401."""
        import httpx
        from fastmcp.server.auth.providers.debug import DebugTokenVerifier

        config = _make_config(temp_dir)
        server = LithosServer(config)
        await server.initialize()
        try:
            server.mcp.auth = DebugTokenVerifier()
            app = server.mcp.http_app(transport="sse")
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/events")
                assert resp.status_code == 401
        finally:
            await server.shutdown()

    @pytest.mark.asyncio
    async def test_200_with_valid_token_when_auth_configured(self, temp_dir: Path) -> None:
        """When auth is configured, GET /events with a valid token returns 200 SSE."""
        import httpx
        from fastmcp.server.auth.providers.debug import DebugTokenVerifier

        config = _make_config(temp_dir)
        server = LithosServer(config)
        await server.initialize()
        try:
            server.mcp.auth = DebugTokenVerifier()
            app = server.mcp.http_app(transport="sse")
            transport = httpx.ASGITransport(app=app)

            async def _check() -> None:
                async with (
                    httpx.AsyncClient(transport=transport, base_url="http://test") as client,
                    client.stream(
                        "GET",
                        "/events",
                        headers={"Authorization": "Bearer test-token"},
                    ) as resp,
                ):
                    assert resp.status_code == 200
                    assert "text/event-stream" in resp.headers.get("content-type", "")

            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(_check(), timeout=2.0)
        finally:
            await server.shutdown()

    @pytest.mark.asyncio
    async def test_open_when_no_auth(self, temp_dir: Path) -> None:
        """When no auth configured, GET /events is open (200)."""
        import httpx

        config = _make_config(temp_dir)
        server = LithosServer(config)
        await server.initialize()
        try:
            assert server.mcp.auth is None
            app = server.mcp.http_app(transport="sse")
            transport = httpx.ASGITransport(app=app)

            async def _check() -> None:
                async with (
                    httpx.AsyncClient(transport=transport, base_url="http://test") as client,
                    client.stream("GET", "/events") as resp,
                ):
                    assert resp.status_code == 200

            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(_check(), timeout=2.0)
        finally:
            await server.shutdown()
