"""Registry of generated artifacts + the ``docs/generated/README.md`` index.

Every artifact written to ``docs/generated/`` must be declared in
:func:`artifacts`. The manifest test asserts the directory contents match the
registry exactly, so a generator that is renamed or stops running turns into a
test failure instead of a silently rotting committed file.
"""

from __future__ import annotations

from dataclasses import dataclass

from tests.guardrail._common import GENERATED_DIR, REPO_ROOT, load_architecture, with_header


@dataclass(frozen=True)
class Artifact:
    path: str  # relative to docs/generated/
    title: str
    description: str  # one line, CONTEXT.md vocabulary


def component_page_paths() -> list[str]:
    """Per-component drill-down pages (one per [components] entry)."""
    return [f"components/{c}.md" for c in sorted(load_architecture()["components"])]


def all_expected_paths() -> set[str]:
    """Every file the generators are expected to produce (for the manifest test)."""
    return {a.path for a in artifacts()} | set(component_page_paths()) | {"README.md"}


def artifacts(arch: dict | None = None) -> list[Artifact]:
    """All generated artifacts, in the order they appear in the index.

    The container view and tool catalog are optional adapters: each appears only
    when its ``docs/architecture.toml`` section is populated, so a project that
    reuses the kit without them gets no empty/orphaned artifact (and the manifest
    test stays exact).
    """
    arch = arch if arch is not None else load_architecture()
    items = [
        Artifact(
            path="architecture.md",
            title="Component dependencies",
            description=(
                "Cross-component import graph computed from the real code via grimp, "
                "grouped by tier (Entrypoints → Core → Foundation)."
            ),
        ),
        Artifact(
            path="domain_model.md",
            title="Domain model",
            description=(
                "Class diagram of the domain dataclasses and Pydantic models, "
                "extracted statically from the modules listed in docs/architecture.toml."
            ),
        ),
    ]
    if arch.get("containers", {}).get("stores"):
        items.append(
            Artifact(
                path="containers.md",
                title="Data stores",
                description=(
                    "The on-disk stores and external engines (corpus, indexes, SQLite DBs) "
                    "with the component that owns each — source of truth vs derived views."
                ),
            )
        )
    if arch.get("tool_catalog", {}).get("include_modules"):
        items.append(
            Artifact(
                path="tool_catalog.md",
                title="MCP tool catalog",
                description=(
                    "The server's public API: every lithos_* tool with its signature, "
                    "one-line summary, and which core components it touches."
                ),
            )
        )
    items += [
        Artifact(
            path="metrics.md",
            title="Architecture metrics",
            description=(
                "Quantitative snapshot (coupling, cycles, size, complexity) with the hard "
                "budgets from docs/architecture.toml — the improving-vs-regressing signal."
            ),
        ),
        Artifact(
            path="metrics.json",
            title="Architecture metrics (machine-readable)",
            description=(
                "Same snapshot as JSON; its git history is the metric time series "
                "(`make metrics-history`)."
            ),
        ),
    ]
    return items


def _usage_lines(present: set[str]) -> list[str]:
    """The 'How to use these' bullets, referencing only artifacts that exist.

    Optional adapters (containers.md, tool_catalog.md) are only linked when their
    artifact is present, so a ported project without them gets no broken links.
    """
    lines = [
        "## How to use these",
        "",
        "- **New to the codebase?** Start at [architecture.md](architecture.md) for the",
        "  component map and click a node to open its drill-down page.",
    ]
    if "containers.md" in present:
        lines += [
            "  [containers.md](containers.md) shows where data lives;",
            "  [domain_model.md](domain_model.md) shows the shapes it takes.",
        ]
    else:
        lines.append("  [domain_model.md](domain_model.md) shows the shapes the data takes.")
    if "tool_catalog.md" in present:
        lines += [
            "- **Building against the server?** [tool_catalog.md](tool_catalog.md) is the",
            "  public API — every tool, its signature, and the components it touches.",
        ]
    lines += [
        "- **Reviewing a PR?** CI posts an architecture-metrics delta in its job summary;",
        "  [metrics.md](metrics.md) has the full snapshot and the budgets that gate",
        "  regressions. `make metrics-history` plots any metric over its commit history.",
    ]
    return lines


def render_index(arch: dict | None = None) -> str:
    arch = arch if arch is not None else load_architecture()
    items = artifacts(arch)
    lines = [
        "# Generated architecture docs",
        "",
        (
            "Everything in this directory is a **derived view of the source code** — "
            + "generated by `tests/guardrail/`, committed, and drift-checked in CI. If the "
            + "code changes shape, the committed view here disagrees with the corpus of "
            + "code and CI fails until it is reconciled: run `make diagrams` and commit "
            + "the result."
        ),
        "",
        "Do not edit these files by hand.",
        "",
    ]
    lines += _usage_lines({a.path for a in items})
    lines += [
        "",
        "## Artifacts",
        "",
        "| Artifact | What it shows |",
        "|---|---|",
    ]
    for a in items:
        lines.append(f"| [{a.title}]({a.path}) | {a.description} |")
    comp_links = " · ".join(
        f"[{path.removeprefix('components/').removesuffix('.md')}]({path})"
        for path in component_page_paths()
    )
    lines += ["", "## Components", "", f"Per-component drill-down pages: {comp_links}"]
    lines += [
        "",
        "## Legend",
        "",
        "- `A --> B` in the component diagram: at least one real `import` from a module",
        "  in component A to a module in component B.",
        "- Tier subgraphs (Entrypoints / Core / Foundation): dependencies must only",
        "  point downward; enforced by import-linter (`pyproject.toml [tool.importlinter]`).",
        "- Dashed grey edge: a tier-skipping dependency (e.g. Entrypoints → Foundation).",
        "  Grey edge: a dependency on a Foundation component (de-emphasized fan-in).",
        "- Component nodes are clickable — they link to the per-component drill-down page.",
        '- `Src "1" --> "0..*" Dst : field` in the domain model: class `Src` has a',
        "  field holding many `Dst`; `0..1` = optional, `1` = exactly one.",
        "",
        "## Sources of truth",
        "",
        "- [`docs/architecture.toml`](../architecture.toml) — components, tiers, and",
        "  which modules are scanned for domain models.",
        "- `pyproject.toml [tool.importlinter]` — the enforced directional contracts.",
    ]
    if (REPO_ROOT / "CONTEXT.md").exists():
        lines.append("- [`CONTEXT.md`](../../CONTEXT.md) — the domain vocabulary used in labels.")
    if (REPO_ROOT / "docs" / "adr").is_dir():
        lines.append("- [`docs/adr/`](../adr/) — the decisions behind the architecture.")
    lines += [
        "",
        "## Regenerating",
        "",
        "```sh",
        "make diagrams   # = pytest tests/guardrail/ -q",
        "```",
        "",
        "Then commit the changes. The CI job `Diagram drift` fails when the committed",
        "files disagree with what the code generates.",
    ]
    return with_header("\n".join(lines) + "\n")


def generated_files() -> set[str]:
    """Relative paths of all files currently present under docs/generated/."""
    return {
        str(p.relative_to(GENERATED_DIR)) for p in sorted(GENERATED_DIR.rglob("*")) if p.is_file()
    }
