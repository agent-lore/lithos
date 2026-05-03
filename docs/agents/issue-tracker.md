# Issue tracker: GitHub

Issues and PRDs for this repo live as GitHub issues on `agent-lore/lithos`. Use the `gh` CLI for all operations.

This clone has two remotes (`origin` → `agent-lore/lithos`, `contributor` → `hanumanclaw/lithos`). Always pin `gh` to the canonical repo with `-R agent-lore/lithos` so commands don't accidentally hit the wrong fork.

## Conventions

- **Create an issue**: `gh issue create -R agent-lore/lithos --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> -R agent-lore/lithos --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list -R agent-lore/lithos --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> -R agent-lore/lithos --body "..."`
- **Apply / remove labels**: `gh issue edit <number> -R agent-lore/lithos --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> -R agent-lore/lithos --comment "..."`

## When a skill says "publish to the issue tracker"

Create a GitHub issue on `agent-lore/lithos`.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> -R agent-lore/lithos --comments`.
