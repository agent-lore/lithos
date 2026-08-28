"""Short id-prefix resolution machinery (task 83257ced).

Humans (and the notes they write) refer to tasks and documents by short id
prefixes; the MCP API stores full ids. This module holds the pure, shared
pieces both resolvers build on — the task side binds :func:`prefix_upper_bound`
into an indexed SQL range query, the note side keeps a :class:`PrefixIndex`
beside the corpus id map. Resolution semantics (exact-first, minimum length,
loud ambiguity) live with the resolvers in ``coordination`` and ``knowledge``.

Everything here is sub-linear by construction: the SQL bound turns a prefix
into an index range scan, and :meth:`PrefixIndex.match` is a bisect plus at
most ``limit`` steps.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable

MIN_PREFIX_LEN = 6
AMBIGUITY_CANDIDATE_CAP = 5

_MAX_CODEPOINT = 0x10FFFF
_SURROGATE_RANGE = (0xD800, 0xDFFF)


def prefix_upper_bound(prefix: str) -> str | None:
    """Smallest string greater than every string starting with ``prefix``.

    Enables the half-open range ``prefix <= s < upper_bound`` — the form a
    BINARY-collated index can range-scan (``LIKE`` cannot, without pragmas).
    Returns ``None`` when no such string exists (every char is at the maximum
    code point); callers then fall back to an open-ended ``s >= prefix``.
    Increments skip the surrogate block so the result stays encodable.
    """
    for i in range(len(prefix) - 1, -1, -1):
        cp = ord(prefix[i])
        if cp < _MAX_CODEPOINT:
            cp += 1
            if _SURROGATE_RANGE[0] <= cp <= _SURROGATE_RANGE[1]:
                cp = _SURROGATE_RANGE[1] + 1
            return prefix[:i] + chr(cp)
    return None


class PrefixIndex:
    """Sorted id list answering prefix queries in O(log n + limit).

    Mutation-safe under the event-loop-only write discipline the corpus index
    already relies on; no interior locking.
    """

    def __init__(self, ids: Iterable[str] = ()) -> None:
        self._ids: list[str] = sorted(ids)

    def __len__(self) -> int:
        return len(self._ids)

    def add(self, id_: str) -> None:
        """Insert ``id_`` keeping sort order; idempotent."""
        i = bisect_left(self._ids, id_)
        if i == len(self._ids) or self._ids[i] != id_:
            self._ids.insert(i, id_)

    def discard(self, id_: str) -> None:
        """Remove ``id_`` if present; missing ids are a no-op."""
        i = bisect_left(self._ids, id_)
        if i < len(self._ids) and self._ids[i] == id_:
            del self._ids[i]

    def rebuild(self, ids: Iterable[str]) -> None:
        """Replace the whole index (authoritative rescan path)."""
        self._ids = sorted(ids)

    def match(self, prefix: str, *, limit: int) -> list[str]:
        """Up to ``limit`` ids starting with ``prefix``, in sorted order."""
        i = bisect_left(self._ids, prefix)
        matches: list[str] = []
        while i < len(self._ids) and len(matches) < limit and self._ids[i].startswith(prefix):
            matches.append(self._ids[i])
            i += 1
        return matches
