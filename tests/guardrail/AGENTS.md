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
- **No implicit string concatenation** when splitting a long string for line
  width — GitHub code quality flags it (it reads as a missing comma in lists).
  Join the pieces with explicit `+` (plain prefix on placeholder-less pieces);
  the hub's drift normalizer folds `+`-joined plain and f-string pieces, so
  this never registers as kit drift.
- Determinism is tested (e.g. `test_metrics_snapshot` renders twice and asserts
  byte-equality). Keep those tests passing.

## No importing the target package at generation time

Everything is **static analysis** — `ast` plus the project's dependency graph.
Importing the package would drag its heavy runtime dependencies into test
collection and risk side effects.
`build_import_graph()` in `_common.py` is the single graph builder and is
`lru_cache`d; reuse it, don't rebuild. It dispatches on `[project] language`:
grimp's import graph for Python (the default), the quoted-`#include` graph from
`_cpp_graph.py` for `language = "cpp"` — both expose the same surface
(`.modules` + `find_modules_directly_imported_by`), so generators stay
language-blind. Python-only views (domain model, public API, class/function
counts) report zeros or are omitted for C++; cyclomatic complexity for C++
comes from `lizard` (pin its version — the committed snapshot embeds its
counting rules, which differ from the Python AST visitor's, so compare
complexity within a language, not across); the layering contract swaps
import-linter for a no-upward-tier-edge check on the include graph.

## Config is the source of truth, not the code

Project identity and every scan list live in `docs/architecture.toml`. **Never
hardcode the package name or its src path** — read `[project]` (`root_package`,
`src_layout`) via `load_architecture()`. The completeness/orphan/manifest tests
fail until the config matches reality, which is the point: adding a module,
component, store, or tool means editing the toml.

Config sections: `[project]`, `[components]`, `[tiers]`, `[domain]`,
`[tool_catalog]`, `[containers]`, `[cpp]`, `[budgets]`, `[component_docs]`.

For C++ projects, `[cpp] virtual_includes` maps generated include-path prefixes
(e.g. protobuf headers emitted into the build dir) to synthetic modules so a
generated seam still appears as a component instead of vanishing as "external".

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

**Ordering is handled by `conftest.py`:** a session-scoped autouse fixture
writes every registered artifact before any test runs, so a single
`make diagrams` succeeds even from an empty `docs/generated/` and the
index/manifest checks never depend on pytest's alphabetical file order. The
driver tests then re-render byte-identically and keep their assertions — if
you add an artifact, add it to `conftest._generate_all()` too.

## Adding a new artifact

1. Write a pure `render_*() -> str` in a `_*.py` helper. Wrap the body in
   `with_header(...)` for Markdown; **do not** for JSON (an HTML comment breaks
   the parse — that's why `write()` doesn't add the header itself).
2. Add a driver `test_*.py` that calls `write("name.md", render_*())`.
3. Register it: append an `Artifact(...)` in `_index.artifacts()` (or extend
   `component_page_paths()` for a whole family), and add the same write to
   `conftest._generate_all()` so fresh runs emit it before validation.
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
| `_private_access.py` | `seams` metrics: cross-module `_private` reaches in src + tests importing src privates (python-only; optional budgets `cross_module_private_refs`, `tests_private_imports`) |
| `_tool_catalog.py` | `tool_catalog.md` (AST scan of `@*.tool()`-decorated handlers) |
| `_containers.py` | `containers.md` (data stores, declared but anchored to `StorageConfig`) |
| `_cpp_graph.py` | quoted-`#include` graph for `language = "cpp"` (grimp duck-type) |
| `_component_pages.py` | `components/<Component>.md` drill-down pages |
| `_index.py` | artifact registry + `README.md` index |
| `test_*.py` | one driver per artifact, plus manifest + budget guards |

For the project-level view of this system (the CI gate, when to edit the toml),
see the "Architecture guardrails & generated docs" section of the repo-root
`AGENTS.md`.
