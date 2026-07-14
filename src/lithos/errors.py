"""Standard error types for Lithos.

This module defines the error hierarchy used across all Lithos components.
New error types should extend ``LithosError`` so callers can catch at the
appropriate level of specificity.
"""

from __future__ import annotations


class LithosError(Exception):
    """Base error for all Lithos operations."""


class SearchBackendError(LithosError):
    """One or more search backends failed.

    Attributes:
        backend_errors: Mapping of backend name to the exception raised,
            e.g. ``{"tantivy": RuntimeError("...")}`` for a single failure or
            ``{"tantivy": exc1, "chroma": exc2}`` when both backends fail.

    Distinguishing this from an empty result is the key contract: callers that
    receive ``SearchBackendError`` know the query *could not* be executed, not
    merely that no matching documents exist.
    """

    def __init__(self, message: str, backend_errors: dict[str, Exception]) -> None:
        super().__init__(message)
        self.backend_errors: dict[str, Exception] = backend_errors

    def __str__(self) -> str:
        detail = ", ".join(f"{backend}: {exc}" for backend, exc in self.backend_errors.items())
        return f"{super().__str__()} [{detail}]"


class SlugCollisionError(ValueError, LithosError):
    """Raised when a slug would collide with an existing document's slug.

    Attributes:
        slug: The slug that caused the collision.
        existing_id: The doc_id that already owns the slug.
    """

    def __init__(self, slug: str, existing_id: str) -> None:
        super().__init__(f"Slug {slug!r} already in use by document {existing_id!r}")
        self.slug = slug
        self.existing_id = existing_id


class IndexingError(LithosError):
    """All search backends failed during a write operation (index or remove).

    Attributes:
        backend_errors: Mapping of backend name to the exception raised.

    Partial failures (one backend succeeds) are logged as warnings and do not
    raise; this error is reserved for *total* failure where no backend could
    complete the operation.
    """

    def __init__(self, message: str, backend_errors: dict[str, Exception]) -> None:
        super().__init__(message)
        self.backend_errors: dict[str, Exception] = backend_errors

    def __str__(self) -> str:
        detail = ", ".join(f"{backend}: {exc}" for backend, exc in self.backend_errors.items())
        return f"{super().__str__()} [{detail}]"


class CorpusScanError(LithosError):
    """An authoritative corpus scan could not read every known document.

    Raised by :meth:`~lithos.knowledge.KnowledgeManager.scan_corpus` when the
    number of documents read back is short of the number the metadata cache
    says exist — i.e. at least one note was unreadable during the scan.

    Distinguishing this from a genuinely smaller corpus is the key contract:
    reconcile treats its scan as the authoritative snapshot and *deletes*
    derived state for anything absent from it, so applying a plan built from a
    partial scan would silently drop the search-index entries, graph nodes, and
    ``derived_from`` edges of documents that still exist. Callers must fail or
    skip rather than reconcile against an incomplete corpus.

    Attributes:
        expected: Documents the metadata cache says exist.
        read: Documents actually read back.
    """

    def __init__(self, expected: int, read: int) -> None:
        self.expected = expected
        self.read = read
        super().__init__(
            f"corpus scan incomplete: read {read} of {expected} documents "
            f"({expected - read} unreadable)"
        )


class CoordinationError(LithosError):
    """A coordination operation failed validation.

    Carries a stable ``code`` and human ``message`` so the MCP layer can map it
    onto the standard ``{"status": "error", "code", "message"}`` envelope without
    re-deriving the reason.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class CognitiveMemoryError(LithosError):
    """Base error for any failure originating inside CognitiveMemory.

    Catch this to handle "the cognitive memory subsystem could not satisfy
    the request" without caring which sub-component failed.
    """


class ScoutFailure(CognitiveMemoryError):
    """A single retrieval scout's backend raised.

    Caught and logged inside CognitiveMemory.retrieve so one bad scout does
    not kill the whole retrieve. Carried as an exception type (rather than a
    log line) so future code can branch on it — e.g. degraded-mode reporting
    in the result envelope.

    Attributes:
        scout: The canonical scout name (e.g. ``"scout_vector"``).
        cause: The underlying exception the scout raised.
    """

    def __init__(self, scout: str, cause: BaseException) -> None:
        super().__init__(f"scout {scout!r} failed: {cause}")
        self.scout = scout
        self.cause = cause


class RetrieveTimeout(CognitiveMemoryError):
    """Retrieve exceeded its configured wall-clock budget.

    Reserved for the Phase A gather timeout (when LcmaConfig grows the knob).
    Defined now so #258/#259/#260 — and a future timeout slice — share one
    base hierarchy.
    """
