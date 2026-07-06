"""Unit tests for the canonical error-envelope constructors."""

from lithos.envelopes import coordination_error_envelope, error_envelope
from lithos.errors import CoordinationError, LithosError


class TestErrorEnvelope:
    def test_canonical_shape_and_key_order(self):
        envelope = error_envelope("doc_not_found", "Document not found: abc")

        assert envelope == {
            "status": "error",
            "code": "doc_not_found",
            "message": "Document not found: abc",
        }
        # Key order is wire contract: dicts serialise in insertion order.
        assert list(envelope) == ["status", "code", "message"]

    def test_supplementary_keys_appended_after_canonical_keys(self):
        envelope = error_envelope("version_conflict", "stale write", current_version=7)

        assert envelope["current_version"] == 7
        assert list(envelope) == ["status", "code", "message", "current_version"]


class TestCoordinationErrorEnvelope:
    def test_maps_code_and_message(self):
        exc = CoordinationError("task_not_found", "Task 'x' not found.")

        assert coordination_error_envelope(exc) == {
            "status": "error",
            "code": "task_not_found",
            "message": "Task 'x' not found.",
        }


class TestCoordinationErrorTaxonomy:
    def test_is_a_lithos_error(self):
        exc = CoordinationError("cycle", "edge would create a cycle")

        assert isinstance(exc, LithosError)
        assert exc.code == "cycle"
        assert exc.message == "edge would create a cycle"
        assert str(exc) == "edge would create a cycle"
