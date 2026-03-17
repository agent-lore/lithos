# Changelog

## Unreleased

### Fix: `lithos_read` returns structured error on missing document (issue #102)

Previously, `lithos_read` propagated a raw `FileNotFoundError` as an
MCP-level exception when the requested id or path did not exist.  This
is the most common failure path and a predictable condition, not a crash.

`lithos_read` now catches `FileNotFoundError` and returns a structured
error envelope:

```json
{ "status": "error", "code": "doc_not_found", "message": "..." }
```

### Fix: Consistent error envelopes across all tools (issue #85)

Error handling was inconsistent across tool categories:

- Coordination tools (`lithos_task_claim`, `lithos_task_renew`,
  `lithos_task_release`, `lithos_task_complete`) returned `{ success: false }`
  on failure.
- `lithos_delete` returned `{ success: false }` when the document was not found.

All failure paths now return a standard error envelope:

```json
{ "status": "error", "code": "...", "message": "..." }
```

| Tool | Error code |
|------|-----------|
| `lithos_delete` (not found) | `doc_not_found` |
| `lithos_task_claim` (task missing/closed/conflict) | `claim_failed` |
| `lithos_task_renew` (no active claim) | `claim_not_found` |
| `lithos_task_release` (no matching claim) | `claim_not_found` |
| `lithos_task_complete` (task missing/not open) | `task_not_found` |

**Breaking:** callers that checked `result.get("success") == False` on
coordination tools must be updated to check `result.get("status") == "error"`.
Success paths are unchanged.

### Breaking: `agent` is now required on `lithos_delete` (issue #80)

For audit-trail consistency, `agent` was optional on `lithos_delete` while
it was required on every other mutation tool (`lithos_write`,
`lithos_task_create`, `lithos_task_claim`, `lithos_task_complete`,
`lithos_finding_post`).

`agent` is now a **required** parameter on `lithos_delete`.  Callers that
omit it will receive a `TypeError` from the MCP layer.  This is a breaking
change intended to land before v1.0.
### Schema change — `version` field in frontmatter (issue #45)

PR #55 adds optimistic locking via a `version` integer field in the YAML
frontmatter of every knowledge document.

**Existing documents** written before this change will be treated as
`version: 1` on first read (the field defaults to `1` when absent). No
migration is needed; the field is added automatically the next time a
document is updated.

**New documents** will have `version: 1` written into their frontmatter
at creation time.

**Breaking (update calls only):** the `lithos_write` MCP tool now accepts
an optional `expected_version` parameter.  If provided and the document's
current version does not match, the call returns a `version_conflict`
error.  Callers that do not pass `expected_version` are unaffected.


