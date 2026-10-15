"""LLM eval suite (``docs/plans/llm-eval-suite.md``).

Lives under ``lithos.lcma`` so it can reuse the engine's pure prompt/parse
functions and the client without crossing the ADR-0005 seam; entrypoints
reach it through the ``lithos.cognitive_memory`` facade. Nothing in this
package writes to the knowledge, edge, or stats stores.
"""
