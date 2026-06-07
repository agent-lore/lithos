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

## Mandatory Fields

| Field | Required | Rules |
|-------|----------|-------|
| `tags` | Yes | Must include `"influx:inbox"` — this is how Influx discovers submissions |
| `metadata.kind` | Yes | `"url"` or `"pdf"` only — anything else is a terminal error |
| `metadata.url` | If kind=url | Must be `http://` or `https://` scheme |
| `metadata.local_path` | If kind=pdf | Must resolve inside configured `pdf_root` |
| `metadata.submitted_by` | Yes | Only `[A-Za-z0-9:._-]` kept, truncated to 64 chars |
| `metadata.source_tag` | Yes | `^[a-z0-9][a-z0-9-]{0,31}$` — lowercase alphanumeric + hyphens, 1–32 chars |

## Optional Fields

| Field | Purpose |
|-------|---------|
| `metadata.title` | Title hint for ingestion |
| `metadata.summary` | Pre-fetched summary to assist profile scoring |

## Validation Rules

1. `influx:inbox` tag is mandatory — submissions without it are invisible to Influx
2. `kind` must be `"url"` or `"pdf"` — no other values accepted
3. URLs must use `http://` or `https://` scheme
4. PDF paths must resolve inside the configured `pdf_root`
5. `source_tag` format: `^[a-z0-9][a-z0-9-]{0,31}$`
6. All submissions must clear each profile's relevance threshold — no bypass
7. Resubmitting the same URL is safe — deduplication scores only un-ingested profiles
8. Rate limit: max 20 items processed per 5-minute tick (configurable)

## Checking Ingestion Results

After submission, poll the task until it completes:

```
lithos_task_get(task_id="<returned-task-id>")
```

When complete, check:
- `outcome` — human-readable result string
- `metadata.inbox_result` — structured JSON with per-profile scores and note IDs
- The created/updated Lithos document IDs will appear in `inbox_result`

## Pitfalls

- **Missing `influx:inbox` tag** — the single most common mistake. Without it, Influx never sees the task
- **Wrong `kind` value** — must be exactly `"url"` or `"pdf"`, lowercase. Any other value is a terminal error and the task will not be processed
- **`source_tag` format** — must match `^[a-z0-9][a-z0-9-]{0,31}$`. Uppercase, underscores, or spaces will fail validation
- **PDF path outside `pdf_root`** — Influx will reject it. Confirm the path is within the configured root before submitting
- **Resubmitting is safe** — if you're unsure whether something was ingested, resubmit. Deduplication handles it
