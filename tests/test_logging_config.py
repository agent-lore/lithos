"""Tests for structured JSON logging (logging_config module).

Verifies:
- Root logger emits valid JSON on every record.
- Standard fields (timestamp, level, logger, message) are present.
- Extra fields (e.g. otelTraceID) are forwarded correctly.
- setup_logging() is idempotent — repeated calls don't duplicate handlers.
- Timestamp uses ISO 8601 format.
"""

from __future__ import annotations

import io
import json
import logging
import re

from lithos.logging_config import _HANDLER_MARKER, LithosJsonFormatter, setup_logging

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_capturing_handler(stream: io.StringIO) -> logging.StreamHandler:  # type: ignore[type-arg]
    """Return a StreamHandler that writes JSON to *stream*, for testing."""
    handler = logging.StreamHandler(stream)
    setattr(handler, _HANDLER_MARKER, True)
    formatter = LithosJsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handler.setFormatter(formatter)
    return handler


def _last_record(stream: io.StringIO) -> dict[str, object]:
    """Parse the last JSON line emitted to *stream*."""
    lines = [ln for ln in stream.getvalue().splitlines() if ln.strip()]
    assert lines, "No log output captured"
    return json.loads(lines[-1])  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# setup_logging() integration tests
# ---------------------------------------------------------------------------


class TestSetupLogging:
    """Tests for the setup_logging() helper."""

    def setup_method(self) -> None:
        """Remove any Lithos JSON handlers installed by previous tests."""
        root = logging.getLogger()
        root.handlers = [h for h in root.handlers if not getattr(h, _HANDLER_MARKER, False)]

    def teardown_method(self) -> None:
        """Clean up handlers after each test."""
        root = logging.getLogger()
        root.handlers = [h for h in root.handlers if not getattr(h, _HANDLER_MARKER, False)]

    def test_installs_handler_on_root_logger(self) -> None:
        buf = io.StringIO()
        setup_logging(stream=buf)
        root = logging.getLogger()
        marked = [h for h in root.handlers if getattr(h, _HANDLER_MARKER, False)]
        assert len(marked) == 1

    def test_idempotent_second_call_no_duplicate_handler(self) -> None:
        buf = io.StringIO()
        setup_logging(stream=buf)
        setup_logging(stream=buf)
        root = logging.getLogger()
        marked = [h for h in root.handlers if getattr(h, _HANDLER_MARKER, False)]
        assert len(marked) == 1

    def test_output_is_valid_json(self) -> None:
        buf = io.StringIO()
        setup_logging(stream=buf)
        logging.getLogger("test.json_valid").info("hello world")
        record = _last_record(buf)
        assert isinstance(record, dict)

    def test_standard_fields_present(self) -> None:
        buf = io.StringIO()
        setup_logging(stream=buf)
        logging.getLogger("test.fields").warning("check fields")
        record = _last_record(buf)
        assert "timestamp" in record
        assert "level" in record
        assert "logger" in record
        assert "message" in record

    def test_message_field_value(self) -> None:
        buf = io.StringIO()
        setup_logging(stream=buf)
        logging.getLogger("test.message").info("my special message")
        record = _last_record(buf)
        assert record["message"] == "my special message"

    def test_level_field_value(self) -> None:
        buf = io.StringIO()
        setup_logging(stream=buf)
        logging.getLogger("test.level").error("oops")
        record = _last_record(buf)
        assert record["level"] == "ERROR"

    def test_logger_field_value(self) -> None:
        buf = io.StringIO()
        setup_logging(stream=buf)
        logging.getLogger("lithos.mymodule").info("hello")
        record = _last_record(buf)
        assert record["logger"] == "lithos.mymodule"

    def test_timestamp_is_iso8601(self) -> None:
        buf = io.StringIO()
        setup_logging(stream=buf)
        logging.getLogger("test.ts").info("timestamp check")
        record = _last_record(buf)
        ts = record["timestamp"]
        assert isinstance(ts, str)
        # ISO 8601: YYYY-MM-DDTHH:MM:SS±HH:MM  (or Z)
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", ts), (
            f"timestamp {ts!r} does not look like ISO 8601"
        )

    def test_no_legacy_keys_in_output(self) -> None:
        """asctime, levelname, name must not appear — they're renamed."""
        buf = io.StringIO()
        setup_logging(stream=buf)
        logging.getLogger("test.nolegacy").info("no legacy keys")
        record = _last_record(buf)
        assert "asctime" not in record
        assert "levelname" not in record
        assert "name" not in record


# ---------------------------------------------------------------------------
# Extra fields (OTEL trace context pass-through)
# ---------------------------------------------------------------------------


class TestExtraFields:
    """Verify that LogRecord extras are serialised into the JSON output."""

    def setup_method(self) -> None:
        root = logging.getLogger()
        root.handlers = [h for h in root.handlers if not getattr(h, _HANDLER_MARKER, False)]

    def teardown_method(self) -> None:
        root = logging.getLogger()
        root.handlers = [h for h in root.handlers if not getattr(h, _HANDLER_MARKER, False)]

    def test_extra_field_otel_trace_id(self) -> None:
        buf = io.StringIO()
        setup_logging(stream=buf)
        logging.getLogger("test.extra").info(
            "with trace",
            extra={"otelTraceID": "abcdef1234567890abcdef1234567890"},
        )
        record = _last_record(buf)
        assert record.get("otelTraceID") == "abcdef1234567890abcdef1234567890"

    def test_extra_field_otel_span_id(self) -> None:
        buf = io.StringIO()
        setup_logging(stream=buf)
        logging.getLogger("test.extra").info(
            "with span",
            extra={"otelSpanID": "1234567890abcdef"},
        )
        record = _last_record(buf)
        assert record.get("otelSpanID") == "1234567890abcdef"

    def test_extra_field_otel_service_name(self) -> None:
        buf = io.StringIO()
        setup_logging(stream=buf)
        logging.getLogger("test.extra").info(
            "with service",
            extra={"otelServiceName": "lithos"},
        )
        record = _last_record(buf)
        assert record.get("otelServiceName") == "lithos"

    def test_extra_field_otel_trace_sampled(self) -> None:
        buf = io.StringIO()
        setup_logging(stream=buf)
        logging.getLogger("test.extra").info(
            "with sampled",
            extra={"otelTraceSampled": True},
        )
        record = _last_record(buf)
        assert record.get("otelTraceSampled") is True

    def test_multiple_extra_fields(self) -> None:
        buf = io.StringIO()
        setup_logging(stream=buf)
        logging.getLogger("test.extra").warning(
            "full trace context",
            extra={
                "otelTraceID": "aaaa" * 8,
                "otelSpanID": "bbbb" * 4,
                "otelServiceName": "lithos",
                "otelTraceSampled": False,
            },
        )
        record = _last_record(buf)
        assert record["otelTraceID"] == "aaaa" * 8
        assert record["otelSpanID"] == "bbbb" * 4
        assert record["otelServiceName"] == "lithos"
        assert record["otelTraceSampled"] is False

    def test_arbitrary_extra_fields(self) -> None:
        """Non-OTEL extras should also pass through."""
        buf = io.StringIO()
        setup_logging(stream=buf)
        logging.getLogger("test.extra").info(
            "custom extra",
            extra={"request_id": "req-42", "user": "alice"},
        )
        record = _last_record(buf)
        assert record.get("request_id") == "req-42"
        assert record.get("user") == "alice"


# ---------------------------------------------------------------------------
# LithosJsonFormatter unit tests
# ---------------------------------------------------------------------------


class TestLithosJsonFormatter:
    """Unit tests for the formatter class in isolation."""

    def _format_record(
        self, msg: str, level: int = logging.INFO, **extra: object
    ) -> dict[str, object]:
        buf = io.StringIO()
        handler = _make_capturing_handler(buf)
        handler.setLevel(logging.DEBUG)
        logger = logging.getLogger(f"test.formatter.{id(self)}")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        try:
            logger.log(level, msg, extra=extra or None)
        finally:
            logger.removeHandler(handler)
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        assert lines
        return json.loads(lines[-1])  # type: ignore[return-value]

    def test_each_record_is_a_single_line(self) -> None:
        buf = io.StringIO()
        handler = _make_capturing_handler(buf)
        handler.setLevel(logging.DEBUG)
        logger = logging.getLogger("test.singleline")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        try:
            logger.info("line one")
            logger.warning("line two")
        finally:
            logger.removeHandler(handler)
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 2
        for line in lines:
            json.loads(line)  # must be valid JSON

    def test_debug_level_string(self) -> None:
        record = self._format_record("debug msg", level=logging.DEBUG)
        assert record["level"] == "DEBUG"

    def test_critical_level_string(self) -> None:
        record = self._format_record("critical msg", level=logging.CRITICAL)
        assert record["level"] == "CRITICAL"
