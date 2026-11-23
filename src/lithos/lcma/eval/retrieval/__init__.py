"""Retrieval eval: ``lithos_search`` (hybrid, semantic) against ``lithos_retrieve``.

Task ``b8a3f918``. A query set is pooled against a copy of a data
directory (``pool``), the pooled query-note pairs are labelled by hand
(``cases``), and the labelled pool is scored per method and per slice
(``scoring``, ``report``). Nothing here writes to a store: retrieval runs
with ``record=False`` and the only output is the case file and the run
document.
"""
