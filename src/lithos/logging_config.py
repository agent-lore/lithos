"""Structured JSON logging configuration for Lithos.

Installs a JSON formatter on the root logger so every log record is
emitted as a single-line JSON object.  Lithos is a server; operators
running locally can pipe stdout/stderr through ``jq`` for readability.

Set the environment variable ``LITHOS_LOG_FORMAT=text`` to use a plain
human-readable formatter instead of JSON.  Useful for local dev or when
using stdio transport where JSON is inconvenient.

The OTEL log bridge already injects ``otelTraceID``, ``otelSpanID``,
``otelServiceName``, and ``otelTraceSampled`` as ``LogRecord`` extras.
Because the JSON formatter serialises all extras, those fields appear
automatically in every log line when OTEL tracing is active — making
trace-log correlation machine-readable with no extra work.

Typical output::

    {"timestamp": "2026-03-31T12:34:56+00:00", "level": "INFO",
     "logger": "lithos.server", "message": "OpenTelemetry initialized",
     "otelTraceID": "0000...0000", "otelSpanID": "0000...0000"}

Usage::

    from lithos.logging_config import setup_logging

    setup_logging()          # configures root logger, idempotent
    setup_logging(level=logging.DEBUG)
    # or, for plain text output:
    # LITHOS_LOG_FORMAT=text lithos serve ...
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

from pythonjsonlogger.json import JsonFormatter as _JsonFormatter

__all__ = ["LithosJsonFormatter", "setup_logging"]

# Sentinel: name of the marker attribute placed on the root logger's first
# handler so setup_logging() can detect its own previous install and
# avoid adding duplicate handlers.
_HANDLER_MARKER = "_lithos_json_handler"


class LithosJsonFormatter(_JsonFormatter):
    """JSON formatter that produces clean, consistent log records.

    Field names:

    * ``timestamp`` — ISO 8601 with UTC offset (``+00:00``)
    * ``level``     — upper-case level name (``INFO``, ``WARNING``, …)
    * ``logger``    — logger name (``lithos.server``, etc.)
    * ``message``   — formatted log message
    * any extras injected by the OTEL trace-context filter or caller

    All other standard ``LogRecord`` attributes (``asctime``, ``levelname``,
    ``name``) are renamed to avoid redundant keys.

    Typical output::

        {"timestamp": "2026-03-31T12:34:56+00:00", "level": "INFO",
         "logger": "lithos.server", "message": "OpenTelemetry initialized"}
    """

    def add_fields(
        self,
        log_data: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        """Populate ``log_data`` with renamed / cleaned-up fields."""
        super().add_fields(log_data, record, message_dict)

        # Always produce the timestamp from the record's created time using
        # datetime.isoformat() so we always get the +00:00 form (not +0000).
        log_data["timestamp"] = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
            timespec="seconds"
        )

        # Remove asctime if present (we replaced it above)
        log_data.pop("asctime", None)

        # Rename levelname → level
        if "levelname" in log_data:
            log_data["level"] = log_data.pop("levelname")

        # Rename name → logger
        if "name" in log_data:
            log_data["logger"] = log_data.pop("name")


def setup_logging(level: int = logging.INFO, stream: Any = None) -> None:
    """Configure the root logger to emit structured JSON (or plain text).

    Idempotent: a second call with the same (or no) arguments is a no-op
    if a Lithos handler is already installed on the root logger.

    Args:
        level:  Root logger level (default: ``logging.INFO``).
        stream: Output stream (default: ``sys.stderr``).  Tests may pass an
                ``io.StringIO`` instance to capture output.

    Environment variables:
        LITHOS_LOG_FORMAT: Set to ``text`` for a plain human-readable
            formatter instead of JSON.  Defaults to ``json``.
    """
    root = logging.getLogger()

    # Idempotency guard with closed-stream recovery.
    #
    # If a Lithos-marked handler already exists *and* its stream is still
    # usable, skip — this is the normal re-entrant call from tests or
    # repeated CLI invocations.
    #
    # But: if the stream has been closed underneath us (for example when a
    # prior Click ``CliRunner.invoke`` call captured ``sys.stderr`` into a
    # buffer that the runner then closed), the pinned handler will raise
    # ``ValueError: I/O operation on closed file`` on the very next log
    # record. In that case we drop the stale handler and install a fresh
    # one. See #173 for the repro.
    survivors: list[logging.Handler] = []
    replace = False
    for handler in root.handlers:
        if getattr(handler, _HANDLER_MARKER, False):
            handler_stream = getattr(handler, "stream", None)
            if handler_stream is not None and getattr(handler_stream, "closed", False):
                replace = True
                # Drop this stale handler — do not preserve it.
                continue
        survivors.append(handler)
    root.handlers = survivors
    if not replace:
        for handler in root.handlers:
            if getattr(handler, _HANDLER_MARKER, False):
                return

    if stream is None:
        stream = sys.stderr

    handler = logging.StreamHandler(stream)
    # Mark so we can detect it on subsequent calls.
    setattr(handler, _HANDLER_MARKER, True)

    log_format = os.environ.get("LITHOS_LOG_FORMAT", "json").lower()
    if log_format == "text":
        formatter: logging.Formatter = logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    else:
        # JSON formatter: no datefmt — timestamp is always produced via
        # datetime.isoformat() in add_fields() to guarantee +00:00 form.
        formatter = LithosJsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    handler.setFormatter(formatter)
    # Only root.setLevel() is needed; handler-level filtering is redundant
    # because the root logger gates all records before they reach handlers.
    root.setLevel(level)
    root.addHandler(handler)
