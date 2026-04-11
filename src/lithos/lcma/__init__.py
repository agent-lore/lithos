"""LCMA (Lithos Cognitive Memory Architecture) package.

This package will grow to contain the scouts, re-ranker, learning layer,
and supporting utilities for Phase 7 (LCMA Rollout). For now it exposes
the shared utility helpers needed as prerequisites before MVP 1 work begins.
"""

from lithos.lcma.scouts import (
    ALL_SCOUT_NAMES,
    SCOUT_EXACT_ALIAS,
    SCOUT_FRESHNESS,
    SCOUT_LEXICAL,
    SCOUT_PROVENANCE,
    SCOUT_TAGS_RECENCY,
    SCOUT_TASK_CONTEXT,
    SCOUT_VECTOR,
)
from lithos.lcma.utils import Candidate, merge_and_normalize

__all__ = [
    "ALL_SCOUT_NAMES",
    "SCOUT_EXACT_ALIAS",
    "SCOUT_FRESHNESS",
    "SCOUT_LEXICAL",
    "SCOUT_PROVENANCE",
    "SCOUT_TAGS_RECENCY",
    "SCOUT_TASK_CONTEXT",
    "SCOUT_VECTOR",
    "Candidate",
    "merge_and_normalize",
]
