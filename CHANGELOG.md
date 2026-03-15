# Changelog

## Unreleased

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


