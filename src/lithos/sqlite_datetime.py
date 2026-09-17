"""Datetime codec for the coordination SQLite stores.

Extracted from ``coordination.py`` so the agent registry (a sibling module
that ``coordination`` imports) can share it without a module cycle.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def parse_datetime(value: str | datetime | None) -> datetime | None:
    """Parse a datetime from SQLite.

    Returns ``None`` for legitimately-missing values (the field is NULL or
    already ``None``) and *also* for values the parser could not interpret.
    Unparseable values are logged at WARNING with the offending raw input so
    that silent data corruption (a partial write, a manual edit, a schema
    mismatch) shows up in operator logs rather than degrading silently to
    ``None`` and then to ``datetime.now()`` at the call site (#205).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        # SQLite stores as ISO format string
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        logger.warning(
            "Failed to parse datetime from SQLite value %r; treating as missing",
            value,
        )
        return None


def format_datetime(dt: datetime) -> str:
    """Format datetime for SQLite."""
    return dt.isoformat()
