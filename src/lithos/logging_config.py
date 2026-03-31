"""Structured JSON logging configuration for Lithos.

Installs a JSON formatter on the root logger so every log record is
emitted as a single-line JSON object.  Lithos is a server; operators
running locally can pipe stdout/stderr through ``jq`` for readability.

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
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any

try:
    # python-json-logger >= 3.2 moved to pythonjsonlogger.json
    from pythonjsonlogger.json import JsonFormatter as _JsonFormatter
except ImportError:  # pragma: no cover
    from pythonjsonlogger.jsonlogger import (
        JsonFormatter as _JsonFormatter,  # pyright: ignore[reportPrivateImportUsage]
    )

__all__ = ["LithosJsonFormatter", "setup_logging"]

# Sentinel: name of the marker attribute placed on the root logger's first
# JsonHandler so setup_logging() can detect its own previous install and
# avoid adding duplicate handlers.
_HANDLER_MARKER = "_lithos_json_handler"


class LithosJsonFormatter(_JsonFormatter):
    """JSON formatter that produces clean, consistent log records.

    Field names:

    * ``timestamp`` — ISO 8601 with UTC offset (``%Y-%m-%dT%H:%M:%S%z``)
    * ``level``     — upper-case level name (``INFO``, ``WARNING``, …)
    * ``logger``    — logger name (``lithos.server``, etc.)
    * ``message``   — formatted log message
    * any extras injected by the OTEL trace-context filter or caller

    All other standard ``LogRecord`` attributes (``asctime``, ``levelname``,
    ``name``) are renamed to avoid redundant keys.
    """

    def add_fields(
        self,
        log_data: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        """Populate ``log_data`` with renamed / cleaned-up fields."""
        super().add_fields(log_data, record, message_dict)

        # Rename asctime → timestamp (already formatted by the handler's datefmt)
        if "asctime" in log_data:
            log_data["timestamp"] = log_data.pop("asctime")
        else:
            # Fallback: produce ISO 8601 from the record's created time.
            log_data["timestamp"] = datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec="seconds")

        # Rename levelname → level
        if "levelname" in log_data:
            log_data["level"] = log_data.pop("levelname")

        # Rename name → logger
        if "name" in log_data:
            log_data["logger"] = log_data.pop("name")


def setup_logging(level: int = logging.INFO, stream: Any = None) -> None:
    """Configure the root logger to emit structured JSON.

    Idempotent: a second call with the same (or no) arguments is a no-op
    if a Lithos JSON handler is already installed on the root logger.

    Args:
        level:  Root logger level (default: ``logging.INFO``).
        stream: Output stream (default: ``sys.stderr``).  Tests may pass an
                ``io.StringIO`` instance to capture output.
    """
    root = logging.getLogger()

    # Idempotency guard: if we already installed our handler, skip.
    for handler in root.handlers:
        if getattr(handler, _HANDLER_MARKER, False):
            return

    if stream is None:
        stream = sys.stderr

    handler = logging.StreamHandler(stream)
    # Mark so we can detect it on subsequent calls.
    setattr(handler, _HANDLER_MARKER, True)

    formatter = LithosJsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handler.setFormatter(formatter)
    handler.setLevel(level)

    root.setLevel(level)
    root.addHandler(handler)
