# Reviewing code quality

The kit records structural measurements and review evidence. It does not assign
one overall quality score. Repository size, language, scope, and measurement rules
matter when interpreting a trend.

## Policies and exceptions

`[budgets]` holds repository-wide ceilings. `[policies]` classifies each budget or
entity threshold as `invariant`, `review`, or `context`. Cycle budgets default to
`invariant`; other keys default to `review`. Explicit dependency contracts remain
invariants. An invariant breach fails generation; review findings remain visible
without requiring a mechanical refactor. This changes the old behavior where
every aggregate ceiling blocked CI: explicitly mark any other mandatory budgets
as invariants when upgrading.

```toml
[policies]
component_cycles = "invariant"
module_cycles = "invariant"
cross_module_private_refs = "invariant"
function_complexity = "review"
function_length = "review"
module_lines = "review"
tier_skipping_edges = "context"

[review]
function_complexity = 10
function_length = 75
module_lines = 800
enforce_new = false
enforce_worsened = false

[[exceptions]]
metric = "function_complexity"
target = "my_package.parser.parse"
limit = 18
reason = "The alternatives belong to one cohesive grammar; extraction obscures it."
review = "PR #123"
```

Exceptions match an exact function/module identity and an individual metric.
They apply only to review policies, require a reason and review reference, and
accept only values at or below their recorded limit. They do not raise the
repository-wide ceiling or hide measured worsening. Reports suggest removing
retired exceptions or reducing unused headroom; they never edit configuration.
Names/paths identify entities: a rename is reported as removal plus addition.
Review those pairs together instead of claiming automatic debt reduction.

Set `[project] layering = "open"` (default) to permit downward dependencies across
multiple tiers. Use `"adjacent"` only when direct dependencies must stay within a
tier or the immediately lower tier. Python import-linter still enforces the
configured dependency restrictions; C++ enforces downward direction directly.

## Upgrade and changed-code enforcement

1. Install the new kit, including `scripts/quality.py`, `_measurement.py`, and
   `docs/quality-guide.md`; regenerate and commit a baseline snapshot.
2. Review policy classifications, measurement scope, and existing exceptions.
3. After the baseline is on main, enable `enforce_new` and/or `enforce_worsened`
   if these review findings must block PRs until addressed or explicitly excepted.
4. Run `python scripts/metrics_diff.py BASE.json HEAD.json --markdown --check`
   in PR CI. Use an immutable PR base commit, not the moving branch tip.

The comparison applies the head thresholds to both revisions. A threshold change
is therefore a policy decision rather than an invented source regression.
Analyzer definition, language, tool, or scope changes prevent verified
new/worsened/resolved judgments. Enabled changed-code gates fail with a baseline
message until comparable function snapshots exist. Old snapshots remain readable;
they do not become fabricated zero values.

`metrics.json` contains all measured functions, module ownership, dependency edges
and their importing modules, domain fields/relationships, accepted policies, and
measurement provenance. Source/config hashes and versions are deterministic;
commit identities and committer timestamps come from Git. No commit SHA is
embedded in its own generated snapshot.

## Revision review and trends

Open a repository's **Compare revisions and review changes** link. Choose any two
commit refs; JSON is available at `/repos/NAME/changes.json?base=REF&head=REF`.
The view shows added/removed dependencies, changed import evidence, component and
module moves, domain changes, individual violations, and policy/budget decisions.
Green diagram edges are added, red dashed edges are removed, and amber edges
have changed importing modules. Malformed or
missing revision data produces an unavailable message, never an inferred gain.

Trend points preserve full commit identity and timestamps. Their spacing is
ordinal, so multiple commits in the same second remain distinct. Missing data and
measurement boundaries break lines. The cross-repo view shows observations
without interpolation; language, scope, and definitions must agree before values
are compared. Hotspots label the legacy unnormalized fallback separately from
churn density multiplied by current module complexity. They are review priorities,
not predictions or progress scores.

## Optional test-effectiveness evidence

Test/source line ratio is a volume statistic; the kit's own test tooling is
excluded. The dashboard instead makes evidence availability and measurement scope
visible. Coverage and mutation tooling is opt-in and runs in the target project's
normal test environment. The hub never executes commands from a monitored repo.

Set `[quality] evidence_path = "docs/quality-evidence.json"`. After running the
relevant tests, normalize their results to this JSON contract:

```json
{
  "source_hash": "copy measurement.source_hash from the tested snapshot",
  "branch_coverage": {
    "covered": 18,
    "total": 20,
    "tool": "coverage.py <version>",
    "scope": "changed branches in parser.py against <base commit>",
    "reference": "CI run URL or committed test report"
  },
  "mutation": {
    "covered": 7,
    "total": 10,
    "tool": "mutation tool <version>",
    "scope": "selected parser functions; 10 valid mutants, 7 killed",
    "reference": "CI run URL or committed mutation report"
  }
}
```

For branch coverage, count executed eligible branches, not lines. For mutation,
count killed mutants out of valid evaluated mutants, excluding invalid mutants.
Describe exclusions, the changed-code baseline, and selected functions in scope.
Only supply the hash of source actually tested; this is an evidence declaration,
not proof that a test runner executed. Source changes make the evidence `stale`;
missing, invalid, and zero-denominator reports never become a percentage. Commit
and regenerate the snapshot after recording evidence. The file must remain
inside the project root and be smaller than 2 MB. No new coverage/mutation tools
or thresholds are imposed on adopting projects.

## Validate usefulness with outcomes

Record a few selected interventions and observations in architecture.toml:

```toml
[[quality.outcomes]]
intervention = "Simplify parser error handling"
measure = "confirmed parser regressions per 20 changes"
before = 4
after = 1
baseline = "20 changes ending at <commit>"
followup = "20 changes ending at <commit>"
reference = "Issue/PR or incident evidence"
```

Use comparable exposure/windows and link the evidence. Useful measures include
confirmed regressions, reverts, or components touched by comparable tasks. These
are recorded observations, not automatically inferred causal effects. The report
shows them next to structural changes so reviewers can ask whether the intervention
helped. Track guardrail-triggered review decisions and regeneration effort during
a pilot before tightening gates across the fleet.

All snapshots and diagrams remain in each project's Git history. External
storage, a separate metrics repository, and automated fleet upgrades are outside
this change.
