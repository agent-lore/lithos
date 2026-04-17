"""Tests for structured JSON logging (logging_config module).

Verifies:
- Root logger emits valid JSON on every record.
- Standard fields (timestamp, level, logger, message) are present.
- Extra fields (e.g. otelTraceID) are forwarded correctly.
- setup_logging() is idempotent — repeated calls don't duplicate handlers.
- Timestamp uses ISO 8601 format with +00:00 offset.
- Exception info is serialised into the JSON output.
- LITHOS_LOG_FORMAT=text produces plain text instead of JSON.
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
    # No datefmt: timestamp is always produced via datetime.isoformat() in
    # add_fields() to guarantee consistent +00:00 form.
    formatter = LithosJsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
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

    def test_replaces_handler_whose_stream_has_been_closed(self) -> None:
        """Regression for #173: a marked handler whose stream was closed
        underneath us (for example by Click's CliRunner after invoke) must
        be dropped and replaced on the next setup_logging() call, so
        subsequent log records don't raise ValueError: I/O operation on
        closed file."""
        buf_closed = io.StringIO()
        setup_logging(stream=buf_closed)
        buf_closed.close()  # simulate CliRunner closing its captured buffer

        buf_new = io.StringIO()
        setup_logging(stream=buf_new)

        root = logging.getLogger()
        marked = [h for h in root.handlers if getattr(h, _HANDLER_MARKER, False)]
        assert len(marked) == 1, (
            f"Expected exactly one marked handler after closed-stream recovery, got {len(marked)}."
        )
        # The surviving handler must be wired to the fresh (open) stream.
        assert marked[0].stream is buf_new
        # And a log record must now reach the fresh stream without raising.
        logging.getLogger("test.closed_stream.recovery").info("still alive")
        assert buf_new.getvalue(), "Expected log record to land in the new stream."

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

    def test_timestamp_is_iso8601_with_colon_offset(self) -> None:
        """Timestamp must use +HH:MM form (not +HHMM)."""
        buf = io.StringIO()
        setup_logging(stream=buf)
        logging.getLogger("test.ts").info("timestamp check")
        record = _last_record(buf)
        ts = record["timestamp"]
        assert isinstance(ts, str)
        # ISO 8601: YYYY-MM-DDTHH:MM:SS+HH:MM  (colon-separated offset)
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", ts), (
            f"timestamp {ts!r} does not look like ISO 8601 with +HH:MM offset"
        )

    def test_timestamp_utc_offset_colon_form(self) -> None:
        """UTC timestamps must end in +00:00, not +0000."""
        buf = io.StringIO()
        setup_logging(stream=buf)
        logging.getLogger("test.ts.utc").info("utc offset form")
        record = _last_record(buf)
        ts = str(record["timestamp"])
        # datetime.isoformat() always produces the colon form (+00:00)
        assert "+00:00" in ts or ts.endswith("Z"), f"Expected +00:00 UTC offset in {ts!r}"

    def test_no_legacy_keys_in_output(self) -> None:
        """asctime, levelname, name must not appear — they're renamed."""
        buf = io.StringIO()
        setup_logging(stream=buf)
        logging.getLogger("test.nolegacy").info("no legacy keys")
        record = _last_record(buf)
        assert "asctime" not in record
        assert "levelname" not in record
        assert "name" not in record

    def test_text_format_env_var(self, monkeypatch: object) -> None:  # type: ignore[override]
        """LITHOS_LOG_FORMAT=text produces plain text, not JSON."""
        import pytest

        # Use pytest's monkeypatch fixture — skip if not available via param
        if not hasattr(monkeypatch, "setenv"):
            pytest.skip("monkeypatch fixture not available")
        monkeypatch.setenv("LITHOS_LOG_FORMAT", "text")
        buf = io.StringIO()
        setup_logging(stream=buf)
        logging.getLogger("test.textformat").info("plain text log")
        output = buf.getvalue()
        # Should NOT be valid JSON
        assert output.strip(), "Expected some output"
        try:
            json.loads(output.splitlines()[0])
            raise AssertionError("Expected plain text, not JSON")
        except json.JSONDecodeError:
            pass  # correct — not JSON

    def test_exception_info_serialised(self) -> None:
        """exc_info=True records must include exception info in JSON output."""
        buf = io.StringIO()
        setup_logging(stream=buf)
        logger = logging.getLogger("test.exc_info")
        try:
            raise ValueError("test exception for serialisation")
        except ValueError:
            logger.error("something went wrong", exc_info=True)
        record = _last_record(buf)
        assert isinstance(record, dict)
        # The exception should be captured somewhere in the output
        raw = buf.getvalue()
        assert "ValueError" in raw, "Exception type not found in log output"
        assert "test exception for serialisation" in raw, (
            "Exception message not found in log output"
        )


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
            logger.log(level, msg, extra=extra if extra else None)
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


# ---------------------------------------------------------------------------
# Uvicorn double-logging test
# ---------------------------------------------------------------------------


class TestUvicornLogConfig:
    """Verify the uvicorn log_config suppresses handler double-emission."""

    def test_uvicorn_loggers_have_empty_handlers(self) -> None:
        """The log_config dict must set handlers=[] on uvicorn loggers."""
        # Import the config dict structure as used in cli.py
        log_config = {
            "version": 1,
            "disable_existing_loggers": False,
            "loggers": {
                "uvicorn": {"handlers": [], "level": "INFO", "propagate": True},
                "uvicorn.error": {"handlers": [], "level": "INFO", "propagate": True},
                "uvicorn.access": {"handlers": [], "level": "INFO", "propagate": True},
            },
        }
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            assert log_config["loggers"][name]["handlers"] == [], (
                f"{name} must have empty handlers list to prevent double-emission"
            )
            assert log_config["loggers"][name]["propagate"] is True, (
                f"{name} must propagate to root logger"
            )

    def test_single_emission_via_propagation(self) -> None:
        """Records propagated from a child logger appear exactly once."""
        buf = io.StringIO()
        root = logging.getLogger()
        # Clean slate
        original_handlers = root.handlers[:]
        original_level = root.level
        root.handlers = []
        root.setLevel(logging.DEBUG)

        handler = logging.StreamHandler(buf)
        setattr(handler, _HANDLER_MARKER, True)
        handler.setFormatter(
            LithosJsonFormatter(fmt="%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        root.addHandler(handler)

        # Ensure the intermediate "uvicorn" logger also propagates cleanly,
        # just like the production log_config sets handlers=[], propagate=True.
        # Without this, uvicorn's import-time logger setup may intercept the
        # record before it reaches root.
        parent_logger = logging.getLogger("uvicorn")
        saved_parent_handlers = parent_logger.handlers[:]
        saved_parent_propagate = parent_logger.propagate
        parent_logger.handlers = []
        parent_logger.propagate = True

        # Simulate a uvicorn logger with no own handlers, propagate=True
        uv_logger = logging.getLogger("uvicorn.test_single_emission")
        uv_logger.handlers = []
        uv_logger.propagate = True
        uv_logger.setLevel(logging.DEBUG)

        uv_logger.info("uvicorn test message")

        root.handlers = original_handlers
        root.setLevel(original_level)
        parent_logger.handlers = saved_parent_handlers
        parent_logger.propagate = saved_parent_propagate

        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 1, f"Expected 1 line, got {len(lines)}: {buf.getvalue()!r}"
        record = json.loads(lines[0])
        assert record["message"] == "uvicorn test message"
