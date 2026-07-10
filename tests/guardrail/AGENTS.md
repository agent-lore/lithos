# tests/guardrail — the architecture-docs generators

This directory is not a normal test suite. **The `test_*.py` files are
generators**: running them (re)writes `docs/generated/` as a side effect, and the
`assert`s are the guardrails that keep those committed views honest. `make
diagrams` is just `pytest tests/guardrail/ -q`; `make test` runs the same tests,
so any test run rewrites `docs/generated/` — commit the result if it changed.

Read this before editing anything here; most of it is not deducible from the code.

## The invariant: deterministic, byte-identical output

CI's `Diagram drift` job fails on any diff between committed `docs/generated/`
and a fresh regeneration. So every generator **must** produce byte-identical
output for identical input:

- **Sort every collection before emitting.** Sets and dict iteration order leak
  otherwise. When two renders disagree byte-for-byte, an unsorted collection is
  the first suspect.
- **No wall-clock, git sha, absolute path, hostname, or env-dependent data** in
  any artifact. Round ratios; `json.dumps(..., indent=2, sort_keys=True)`.
- Determinism is tested (e.g. `test_metrics_snapshot` renders twice and asserts
  byte-equality). Keep those tests passing.

## No importing `lithos` at generation time

Everything is **static analysis** — `ast` plus grimp's import graph. Importing
the package would drag ChromaDB/torch into test collection and risk side effects.
`build_import_graph()` in `_common.py` is the single grimp builder and is
`lru_cache`d; reuse it, don't rebuild.

## Config is the source of truth, not the code

Project identity and every scan list live in `docs/architecture.toml`. **Never
hardcode `lithos` or `src/lithos`** — read `[project]` (`root_package`,
`src_layout`) via `load_architecture()`. The completeness/orphan/manifest tests
fail until the config matches reality, which is the point: adding a module,
component, store, or tool means editing the toml.

Config sections: `[project]`, `[components]`, `[tiers]`, `[domain]`,
`[tool_catalog]`, `[containers]`, `[budgets]`, `[component_docs]`.

The **tool catalog** (`[tool_catalog]`) and **container view** (`[containers]`)
are optional adapters: their driver tests skip and their artifacts are omitted
when the section is absent, and their Lithos-specific bits (tool prefix, tool
floor, closure var, the `StorageConfig` anchor) are config, not code. The other
views run from `[project]` + `[components]`/`[tiers]` + `[domain]` alone.

## The registry is the manifest

Every file under `docs/generated/` must be produced by a generator **and**
accounted for in `_index.all_expected_paths()`. `test_generated_manifest`
asserts the directory equals the registry, so an orphaned or renamed artifact is
a failure, not a silently rotting file.

**First-run ordering gotcha:** on a *fresh* port with an empty `docs/generated/`,
pytest runs `test_generated_index` / `test_generated_manifest` before the
generators that create the files (files run in alphabetical order), so the
*first* `make diagrams` can fail on missing artifacts; a second run passes. A
committed repo never hits this (the files already exist) — it only matters the
very first time you generate on a new project.

## Adding a new artifact

1. Write a pure `render_*() -> str` in a `_*.py` helper. Wrap the body in
   `with_header(...)` for Markdown; **do not** for JSON (an HTML comment breaks
   the parse — that's why `write()` doesn't add the header itself).
2. Add a driver `test_*.py` that calls `write("name.md", render_*())`.
3. Register it: append an `Artifact(...)` in `_index.artifacts()` (or extend
   `component_page_paths()` for a whole family).
4. `make diagrams` now emits it and CI drift-gates it automatically.

## Budgets are hand-edited ratchets

`[budgets]` are hard counts; `test_metrics_budgets` fails CI on breach. CI must
**never** auto-rewrite them — they are a source-of-truth file, and raising one is
a deliberate, reviewed line in the PR diff (the message names both remediation
paths). `test_budget_keys_are_known` catches a typo'd key so a ratchet can't be
silently disabled.

## Gotcha: `metrics.test_lines` is self-referential

The metrics snapshot counts lines under `tests/`, and this toolkit lives under
`tests/` — so editing any generator here bumps `metrics.json`/`metrics.md`.
Always regenerate and commit after touching the kit.

## Module map

| File | Produces / provides |
|------|---------------------|
| `_common.py` | shared plumbing: paths, `load_architecture`, `component_of`, `module_name_of`, memoized `build_import_graph`, `write`, `with_header` |
| `_diagram_toolkit.py` | `architecture.md` (component diagram, grimp) + `domain_model.md` (AST class scan) |
| `_metrics_toolkit.py` / `_metrics_render.py` | compute metrics / render to `metrics.json` + `metrics.md` |
| `_tool_catalog.py` | `tool_catalog.md` (AST scan of `@*.tool()`-decorated handlers) |
| `_containers.py` | `containers.md` (data stores, declared but anchored to `StorageConfig`) |
| `_component_pages.py` | `components/<Component>.md` drill-down pages |
| `_index.py` | artifact registry + `README.md` index |
| `test_*.py` | one driver per artifact, plus manifest + budget guards |

For the project-level view of this system (the CI gate, when to edit the toml),
see the "Architecture guardrails & generated docs" section of the repo-root
`AGENTS.md`.
