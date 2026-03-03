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
