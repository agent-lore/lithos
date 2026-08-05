"""Tests for the OpenAI-compatible LLM synthesis client (WS1, lithos.lcma.llm).

All transport is faked with httpx.MockTransport — no network, no LLM. The client's
contract under test: request shape (auth, response_format, model), token accounting
(usage vs chars//4 estimation), and failure semantics (retry-once on transient,
immediate LlmError on 4xx, LlmError after exhausted retry).
"""

from __future__ import annotations

import json
import traceback
from typing import Any

import httpx
import pytest

from lithos.config import LlmConfig
from lithos.errors import LlmError
from lithos.lcma.llm import ChatMessage, LlmClient, LlmResult

MESSAGES = [ChatMessage("system", "You classify."), ChatMessage("user", "note text")]


def _config(**overrides: Any) -> LlmConfig:
    defaults: dict[str, Any] = {"base_url": "http://ollama:11434/v1", "model": "qwen3"}
    defaults.update(overrides)
    return LlmConfig(**defaults)


def _ok_response(
    content: str = '{"judgements": []}', usage: dict[str, int] | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return body


def _client_with(handler: Any, **config_overrides: Any) -> LlmClient:
    return LlmClient(_config(**config_overrides), transport=httpx.MockTransport(handler))


async def test_request_shape_includes_model_and_json_format() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_ok_response())

    client = _client_with(handler)
    try:
        await client.chat(MESSAGES)
    finally:
        await client.close()

    assert seen["url"] == "http://ollama:11434/v1/chat/completions"
    assert seen["body"]["model"] == "qwen3"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["messages"][0] == {"role": "system", "content": "You classify."}
    assert seen["auth"] is None  # no api_key -> no Authorization header


async def test_bearer_header_sent_when_api_key_set() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_ok_response())

    client = _client_with(handler, api_key="sk-secret")
    try:
        await client.chat(MESSAGES)
    finally:
        await client.close()
    assert seen["auth"] == "Bearer sk-secret"


async def test_usage_tokens_extracted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_ok_response(usage={"prompt_tokens": 120, "completion_tokens": 30}),
        )

    client = _client_with(handler)
    try:
        result = await client.chat(MESSAGES)
    finally:
        await client.close()
    assert result == LlmResult(
        text='{"judgements": []}', prompt_tokens=120, completion_tokens=30, estimated=False
    )
    assert result.total_tokens == 150


async def test_missing_usage_falls_back_to_estimate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_response(content="x" * 400))

    client = _client_with(handler)
    try:
        result = await client.chat(MESSAGES)
    finally:
        await client.close()
    assert result.estimated
    assert result.completion_tokens == 100  # 400 chars // 4
    assert result.prompt_tokens == sum(len(m.content) for m in MESSAGES) // 4


async def test_transient_5xx_retries_once_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=_ok_response())

    client = _client_with(handler)
    try:
        result = await client.chat(MESSAGES)
    finally:
        await client.close()
    assert calls["n"] == 2
    assert result.text == '{"judgements": []}'


async def test_persistent_timeout_raises_llm_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    client = _client_with(handler)
    try:
        with pytest.raises(LlmError, match="after retry"):
            await client.chat(MESSAGES)
    finally:
        await client.close()


async def test_4xx_fails_immediately_without_retry() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="model not found")

    client = _client_with(handler)
    try:
        with pytest.raises(LlmError, match="404"):
            await client.chat(MESSAGES)
    finally:
        await client.close()
    assert calls["n"] == 1


async def test_malformed_body_raises_llm_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    client = _client_with(handler)
    try:
        with pytest.raises(LlmError, match="Malformed"):
            await client.chat(MESSAGES)
    finally:
        await client.close()


def test_client_requires_base_url() -> None:
    with pytest.raises(ValueError, match="base_url"):
        LlmClient(LlmConfig())


async def test_read_error_becomes_llm_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection reset mid-body")

    client = _client_with(handler)
    try:
        with pytest.raises(LlmError, match="after retry"):
            await client.chat(MESSAGES)
    finally:
        await client.close()


async def test_remote_protocol_error_becomes_llm_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("server disconnected without response")

    client = _client_with(handler)
    try:
        with pytest.raises(LlmError):
            await client.chat(MESSAGES)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": None, "completion_tokens": 5},  # null
        {"prompt_tokens": "many", "completion_tokens": 5},  # non-numeric
        {"prompt_tokens": -10, "completion_tokens": 5},  # negative
        {"prompt_tokens": 100},  # completion missing
    ],
)
async def test_invalid_usage_falls_back_to_estimate(usage: dict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_response(content="x" * 40, usage=usage))

    client = _client_with(handler)
    try:
        result = await client.chat(MESSAGES)
    finally:
        await client.close()
    assert result.estimated
    assert result.completion_tokens == 10  # 40 chars // 4, not the bogus usage


async def test_json_response_false_omits_response_format() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_response(content="free text"))

    client = _client_with(handler)
    try:
        result = await client.chat(MESSAGES, json_response=False)
    finally:
        await client.close()
    assert "response_format" not in seen["body"]  # type: ignore[operator]
    assert result.text == "free text"


async def test_total_tokens_only_usage_is_authoritative() -> None:
    """Providers reporting only an aggregate total are measured, not estimated."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_response(usage={"total_tokens": 137}))

    client = _client_with(handler)
    try:
        result = await client.chat(MESSAGES)
    finally:
        await client.close()
    assert not result.estimated
    assert result.total_tokens == 137


async def test_partial_split_with_total_prefers_total() -> None:
    """A broken split (null completion) with a valid total uses the total."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_ok_response(
                usage={"prompt_tokens": 100, "completion_tokens": None, "total_tokens": 120}
            ),
        )

    client = _client_with(handler)
    try:
        result = await client.chat(MESSAGES)
    finally:
        await client.close()
    assert not result.estimated
    assert result.total_tokens == 120


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": 1.9, "completion_tokens": 5},  # fractional
        {"prompt_tokens": float("nan"), "completion_tokens": 5},
        {"prompt_tokens": float("inf"), "completion_tokens": 5},
        {"prompt_tokens": float("-inf"), "completion_tokens": 5},
        {"total_tokens": 1.9},  # fractional aggregate
        {"total_tokens": float("nan")},
    ],
)
async def test_fractional_and_nonfinite_usage_falls_back_to_estimate(usage: dict) -> None:
    """Round-2 review: fractional values must not truncate the ledger and
    non-finite values must not raise past the LlmError boundary.

    The body is serialized with allow_nan (emitting bare NaN/Infinity tokens,
    which real decoders and Python's json.loads accept) because httpx's strict
    json= kwarg refuses to encode them — the wire shape is realistic even
    though it is not strict JSON."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.dumps(_ok_response(content="x" * 40, usage=usage))
        return httpx.Response(200, text=body, headers={"content-type": "application/json"})

    client = _client_with(handler)
    try:
        result = await client.chat(MESSAGES)
    finally:
        await client.close()
    assert result.estimated
    assert result.completion_tokens == 10  # chars//4 estimate, not the bogus usage


async def test_local_protocol_error_never_echoes_header_bytes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Round-4 review: h11 embeds the raw offending header bytes (Authorization
    included) in LocalProtocolError. Neither the retry warning nor the LlmError
    message/cause chain may carry them — the error is reduced to its type name.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.LocalProtocolError("Illegal header value b'Bearer sk-secret'")

    client = _client_with(handler, api_key="sk-secret")
    try:
        with (
            caplog.at_level("WARNING", logger="lithos.lcma.llm"),
            pytest.raises(LlmError) as excinfo,
        ):
            await client.chat(MESSAGES)
    finally:
        await client.close()
    assert "LocalProtocolError" in str(excinfo.value)
    assert excinfo.value.__cause__ is None  # no chained cause for a traceback to print
    rendered = "".join(traceback.format_exception(excinfo.value))
    assert "sk-secret" not in rendered
    assert all("sk-secret" not in record.getMessage() for record in caplog.records)


async def test_transport_error_message_redacts_api_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Any transport error whose message happens to echo the key (proxy chatter,
    request dumps) is redacted before logging or raising."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("proxy rejected credential 'Bearer sk-secret'")

    client = _client_with(handler, api_key="sk-secret")
    try:
        with (
            caplog.at_level("WARNING", logger="lithos.lcma.llm"),
            pytest.raises(LlmError) as excinfo,
        ):
            await client.chat(MESSAGES)
    finally:
        await client.close()
    assert "[redacted]" in str(excinfo.value)
    assert excinfo.value.__cause__ is None  # raw ConnectError must not ride the cause chain
    rendered = "".join(traceback.format_exception(excinfo.value))
    assert "sk-secret" not in rendered
    assert all("sk-secret" not in record.getMessage() for record in caplog.records)


async def test_4xx_body_echoing_api_key_is_redacted() -> None:
    """A 4xx body that echoes the credential (proxy auth chatter) is redacted
    before its first 200 chars are embedded in the LlmError message."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="rejected credential 'Bearer sk-secret' for upstream")

    client = _client_with(handler, api_key="sk-secret")
    try:
        with pytest.raises(LlmError, match="401") as excinfo:
            await client.chat(MESSAGES)
    finally:
        await client.close()
    assert "[redacted]" in str(excinfo.value)
    rendered = "".join(traceback.format_exception(excinfo.value))
    assert "sk-secret" not in rendered


async def test_integral_float_usage_is_accepted() -> None:
    """JSON decoders may deliver 120.0 — integral finite floats are measured."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_ok_response(usage={"prompt_tokens": 120.0, "completion_tokens": 30.0})
        )

    client = _client_with(handler)
    try:
        result = await client.chat(MESSAGES)
    finally:
        await client.close()
    assert not result.estimated
    assert result.total_tokens == 150
