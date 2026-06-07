---
name: influx-inbox
description: |
  Use when an agent needs to submit a URL or PDF to the Influx inbox for ingestion into the Lithos knowledge base. Covers creating the required influx:inbox task, mandatory metadata fields (kind, url/local_path, submitted_by, source_tag), validation rules, and how to check ingestion results.
---

# Influx Inbox

The Influx inbox lets agents submit URLs or PDFs for ingestion into the Lithos knowledge base. The interface is a Lithos task with specific tags and metadata — Influx polls for tasks tagged `influx:inbox` and processes them.

## Trigger Conditions

Load this skill when you need to:
- Submit a URL or article for ingestion into Lithos
- Submit a local PDF for ingestion into Lithos
- Check the result of a previously submitted ingestion task
- Triage or score content against Influx profiles

Do NOT use this skill for general Lithos knowledge or task work — load the `lithos` skill for that.

## End-to-End Workflow

1. **Create the inbox task** — use `lithos_task_create` with `tags=["influx:inbox"]` and required metadata (see templates below)
2. **Capture the task ID** — store the returned `task_id` from the response
3. **Poll until complete** — `lithos_task_get(task_id="<id>")` until `status == "completed"` or `"cancelled"`
4. **Check the outcome** — read `outcome` (human-readable) and `metadata.inbox_result` (structured, per-profile scores and note IDs)
5. **If failed** — check `metadata.inbox_result` for the error detail, fix the submission (e.g. bad URL scheme, wrong source_tag format), and resubmit

## Submitting a URL

```
lithos_task_create(
    title="Influx inbox: <descriptive title or URL>",
    agent="<your-agent-id>",
    tags=["influx:inbox"],
    metadata={
        "kind": "url",
        "url": "https://example.com/interesting-article",
        "submitted_by": "<your-agent-id>",
        "title": "Optional title hint",
        "summary": "Optional pre-fetched summary",
        "source_tag": "inbox"
    }
)
```

## Submitting a PDF

```
lithos_task_create(
    title="Influx inbox: <filename>",
    agent="<your-agent-id>",
    tags=["influx:inbox"],
    metadata={
        "kind": "pdf",
        "local_path": "/path/under/pdf_root/research-paper.pdf",
        "submitted_by": "<your-agent-id>",
        "source_tag": "papers"
    }
)
```

## Mandatory Fields and Validation

| Field | Required | Validation |
|-------|----------|------------|
| `tags` includes `"influx:inbox"` | Yes | Missing tag = invisible to Influx |
| `metadata.kind` | Yes | Must be exactly `"url"` or `"pdf"` — terminal error otherwise |
| `metadata.url` | If kind=url | Must be `http://` or `https://` scheme |
| `metadata.local_path` | If kind=pdf | Must resolve inside configured `pdf_root` |
| `metadata.submitted_by` | Yes | Only `[A-Za-z0-9:._-]` kept, truncated to 64 chars |
| `metadata.source_tag` | Yes | `^[a-z0-9][a-z0-9-]{0,31}$` — lowercase alphanumeric + hyphens, 1–32 chars |

Optional: `metadata.title` (title hint), `metadata.summary` (pre-fetched summary to assist profile scoring).

**Other constraints:**
- All submissions must clear each profile's relevance threshold — no bypass
- Resubmitting the same URL is safe — deduplication scores only un-ingested profiles
- Rate limit: max 20 items processed per 5-minute tick (configurable)

## Pitfalls

- **Missing `influx:inbox` tag** — the single most common mistake. Without it, Influx never sees the task
- **Wrong `kind` value** — must be exactly `"url"` or `"pdf"`, lowercase. Any other value is a terminal error
- **`source_tag` format** — must match `^[a-z0-9][a-z0-9-]{0,31}$`. Uppercase, underscores, or spaces will fail
- **PDF path outside `pdf_root`** — Influx will reject it. Confirm the path is within the configured root
- **Resubmitting is safe** — if unsure whether something was ingested, resubmit; deduplication handles it
