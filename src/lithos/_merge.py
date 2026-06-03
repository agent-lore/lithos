"""Shared pure helpers for additive metadata merges.

Both task coordination (#290) and document metadata (#305) need the same
read-merge-write semantics for free-form key/value dicts. Keeping the merge
in one place guarantees tasks and notes behave identically.
"""

from typing import Any


def merge_metadata(existing: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Apply an additive per-key patch to a metadata dict.

    Keys in ``patch`` whose value is ``None`` are removed from the result
    (silently if absent). Keys with any other value overwrite the existing
    entry. Keys present in ``existing`` but absent from ``patch`` are
    preserved. ``patch == {}`` is a no-op that returns a fresh copy of
    ``existing``.

    Pure: returns a new dict; neither argument is mutated. Callers relying
    on a multi-writer guarantee (#290) must invoke this inside an atomic
    read-merge-write section so the cycle cannot interleave.
    """
    merged = dict(existing)
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged
