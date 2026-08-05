"""Async OpenAI-compatible chat-completions client for background LLM synthesis (WS1).

One deliberately small client, no SDK: a POST to ``{base_url}/chat/completions`` via
httpx covers Ollama/llama.cpp/vLLM ``/v1`` shims and hosted OpenAI-compatible APIs
alike. Used only by the ``lithos-enrich`` background worker — never on the retrieve
hot path — so the design bias is resilience over throughput: generous configurable
timeout, one retry on transient failures, and every failure surfaced as
:class:`lithos.errors.LlmError` for the caller to swallow.

Token accounting feeds the daily budget ledger (``stats.db`` ``llm_budget``): the
``usage`` block of the response is authoritative; when an endpoint omits it, a
chars//4 heuristic estimate is used and flagged so telemetry can distinguish
measured from estimated spend.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Any

import httpx

from lithos.config import LlmConfig
from lithos.errors import LlmError

logger = logging.getLogger(__name__)

#: Single retry after this pause on timeout / connect error / 5xx.
_RETRY_DELAY_SECONDS = 0.5

#: Rough tokens-per-character heuristic when the endpoint omits ``usage``.
_CHARS_PER_TOKEN = 4


def _usage_int(value: object) -> int | None:
    """Coerce a ``usage`` field to a non-negative int; ``None`` if it isn't one.

    Accepts ints and *integral* finite floats (JSON decoders may deliver
    ``120.0``). Fractional, NaN, and ±infinity values return ``None`` — the
    caller falls back to the flagged estimate — rather than silently
    truncating the budget ledger or letting ``int()`` raise past the
    LlmError boundary.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not (math.isfinite(value) and value.is_integer()):
            return None
    elif not isinstance(value, int):
        return None
    coerced = int(value)
    return coerced if coerced >= 0 else None


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class LlmResult:
    """Completion text plus the token spend that produced it."""

    text: str
    prompt_tokens: int
    completion_tokens: int
    #: True when the endpoint omitted ``usage`` and tokens are a chars//4 estimate.
    estimated: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LlmClient:
    """Thin async client over one OpenAI-compatible ``/chat/completions`` endpoint.

    ``transport`` exists purely as a test seam (``httpx.MockTransport``); production
    callers construct with config only. The client owns its ``httpx.AsyncClient`` —
    call :meth:`close` on worker shutdown.
    """

    def __init__(self, config: LlmConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        if config.base_url is None:
            raise ValueError("LlmClient requires lcma.llm.base_url to be set")
        self._config = config
        self._api_key = config.api_key.get_secret_value() if config.api_key else None
        headers = {}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        self._client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            headers=headers,
            timeout=config.timeout_seconds,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def chat(self, messages: list[ChatMessage], *, json_response: bool = True) -> LlmResult:
        """POST one chat completion; raise :class:`LlmError` on any failure.

        Retries once on timeout, connect error, or 5xx; 4xx fails immediately
        (a bad request will not get better by retrying).
        """
        body: dict[str, Any] = {
            "model": self._config.model,
            "max_tokens": self._config.max_output_tokens,
            "temperature": 0.1,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if json_response:
            # Maps to Ollama's format=json on its /v1 shim; parsers must still
            # treat the output as untrusted.
            body["response_format"] = {"type": "json_object"}

        response = await self._post_with_retry(body)
        return self._parse_response(response, request_chars=sum(len(m.content) for m in messages))

    def _redact(self, text: str) -> str:
        """Strip the api_key value out of any text bound for logs or LlmError."""
        if self._api_key is not None:
            text = text.replace(self._api_key, "[redacted]")
        return text

    def _sanitize_error(self, error: Exception) -> str:
        """Render a transport failure for logs/LlmError without credential bleed.

        ``httpx.LocalProtocolError`` (h11) embeds the raw offending header
        bytes — Authorization included — so it is reduced to its type name.
        Every other message additionally has the api_key value redacted in
        case a proxy or transport echoes request data. Config validation
        already rejects header-unsafe keys, so this is defense in depth.
        """
        if isinstance(error, httpx.LocalProtocolError):
            return type(error).__name__
        return self._redact(f"{type(error).__name__}: {error}")

    async def _post_with_retry(self, body: dict[str, Any]) -> httpx.Response:
        # Catch httpx.HTTPError — the base of every transport/protocol failure
        # (timeouts, connect/read/write errors, RemoteProtocolError,
        # UnsupportedProtocol, invalid URLs) — so nothing escapes the LlmError
        # contract. All of them get the single retry: one wasted 0.5s pause on
        # a non-transient failure is cheaper than misclassifying a transient one.
        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                response = await self._client.post("/chat/completions", json=body)
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if response.status_code < 500:
                    if response.status_code >= 400:
                        # Redact BEFORE truncating: cutting first could leave a
                        # partial key that .replace() no longer matches.
                        detail = self._redact(response.text)[:200]
                        raise LlmError(f"LLM endpoint returned {response.status_code}: {detail}")
                    return response
                last_error = LlmError(f"LLM endpoint returned {response.status_code}")
            if attempt == 1:
                logger.warning(
                    "LLM call failed (%s); retrying once", self._sanitize_error(last_error)
                )
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
        assert last_error is not None  # both loop arms set it or exit the method
        # NEVER chain the transport exception as __cause__: str(LlmError) is
        # sanitized, but traceback.format_exception() renders the cause's raw
        # args — h11's header bytes for LocalProtocolError, proxy chatter that
        # may echo the credential for anything else. The sanitized message
        # already carries the original type name and redacted detail.
        raise LlmError(f"LLM call failed after retry: {self._sanitize_error(last_error)}") from None

    def _parse_response(self, response: httpx.Response, *, request_chars: int) -> LlmResult:
        try:
            payload = response.json()
            text = payload["choices"][0]["message"]["content"]
        except (ValueError, LookupError, TypeError) as exc:
            raise LlmError(f"Malformed LLM response body: {exc}") from exc
        if not isinstance(text, str):
            raise LlmError("Malformed LLM response: message content is not a string")

        usage = payload.get("usage")
        if isinstance(usage, dict):
            prompt_tokens = _usage_int(usage.get("prompt_tokens"))
            completion_tokens = _usage_int(usage.get("completion_tokens"))
            # Both fields must be valid non-negative ints; a null / non-numeric /
            # negative usage block falls through rather than escaping as a raw
            # TypeError or corrupting the ledger.
            if prompt_tokens is not None and completion_tokens is not None:
                return LlmResult(
                    text=text,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            # Some providers report only an aggregate total. The total is what
            # the budget ledger consumes, so it is authoritative (not an
            # estimate); the prompt/completion split is simply unavailable and
            # recorded as total/0.
            total_tokens = _usage_int(usage.get("total_tokens"))
            if total_tokens is not None:
                return LlmResult(text=text, prompt_tokens=total_tokens, completion_tokens=0)
        return LlmResult(
            text=text,
            prompt_tokens=request_chars // _CHARS_PER_TOKEN,
            completion_tokens=len(text) // _CHARS_PER_TOKEN,
            estimated=True,
        )
