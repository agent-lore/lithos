# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker (`agent-lore/lithos`).

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

## Label availability

As of setup, only `wontfix` exists on `agent-lore/lithos`. The other four (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`) need to be created on first use — either upfront with `gh label create -R agent-lore/lithos <name> --description "..."`, or `gh issue edit --add-label` will fail and prompt you to create them.

Edit the right-hand column to match whatever vocabulary you actually use.
