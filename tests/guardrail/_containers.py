"""Generate the container / data-store view (C4-container style).

``docs/generated/containers.md`` — a Mermaid ``graph LR`` of the on-disk stores
and external engines, each linked to the component that owns it, grouped by role
using the CONTEXT.md vocabulary: the corpus is the source of truth; derived
views are rebuilt from it (drift → reconcile); agent/coordination state stands
alone.

The topology is declared in ``docs/architecture.toml [containers]`` (imports
can't reveal it) but anchored to real ``StorageConfig`` properties, which
``test_container_diagram`` cross-checks in both directions.
"""

from __future__ import annotations

from tests.guardrail._common import load_architecture, with_header

# role -> section heading, in display order
ROLE_GROUPS: list[tuple[str, str]] = [
    ("source_of_truth", "Corpus (source of truth)"),
    ("derived_view", "Derived views (rebuilt from the corpus)"),
    ("agent_state", "Agent & coordination state"),
]
KNOWN_ROLES = {role for role, _ in ROLE_GROUPS}


def stores() -> list[dict]:
    cfg = load_architecture().get("containers", {})
    return sorted(cfg.get("stores", []), key=lambda s: s["id"])


def _node_label(store: dict) -> str:
    label = store["label"]
    engine = store.get("engine")
    return f"{label} ({engine})" if engine else label


def render_container_diagram() -> str:
    declared = stores()
    anchor = load_architecture().get("containers", {}).get("anchor", {})
    anchored = f"anchored to `{anchor['class']}`" if anchor.get("class") else "declared here"
    lines = [
        "# Data stores",
        "",
        "On-disk stores and external engines, with the component that owns each. "
        "The corpus is the source of truth; derived views are rebuilt from it "
        "(dashed = derived / reconciled); agent & coordination state stands "
        f"alone. Declared in `docs/architecture.toml` `[containers]`, {anchored}.",
        "",
        "```mermaid",
        "graph LR",
    ]

    for role, title in ROLE_GROUPS:
        members = [s for s in declared if s.get("role") == role]
        if not members:
            continue
        lines.append(f'  subgraph role_{role}["{title}"]')
        for store in members:
            lines.append(f'    {store["id"]}[("{_node_label(store)}")]')
        lines.append("  end")

    for store in declared:
        lines.append(f"  {store['owner']} --> {store['id']}")
    for store in declared:
        if store.get("derived_from"):
            lines.append(f"  {store['derived_from']} -.->|derived / reconciled| {store['id']}")

    lines.append("```")
    return with_header("\n".join(lines) + "\n")
