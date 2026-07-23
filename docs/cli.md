# Lithos CLI Reference

The `lithos` command-line tool lets humans and scripts interact with the Lithos knowledge base directly — without going through an MCP client.

## Installation

The CLI is included with the `lithos-mcp` package. After installation, the `lithos` command is available in your environment.

| Method | Command |
|---|---|
| **Development install** (editable) | `pip install -e .` |
| **From PyPI** | `pip install lithos-mcp` |
| **With uv** | `uv pip install lithos-mcp` |
| **Docker** | included in the container image |

After installation the `lithos` binary is placed in the venv's `bin/` directory (e.g. `/opt/venv/bin/lithos`).

## How It Works

The entry point is declared in `pyproject.toml`:

```toml
[project.scripts]
lithos = "lithos.cli:main"
```

This creates a `lithos` executable that calls `main()` in `src/lithos/cli.py`, which is built with [Click](https://click.palletsprojects.com/).

## Global Options

These options apply to all commands:

```
lithos [OPTIONS] COMMAND [ARGS]...

Options:
  -c, --config PATH     Path to config file (YAML)
  -d, --data-dir PATH   Data directory path
  --telemetry-console   Route OTEL metrics + spans to stdout (local debugging
                        without a collector)
  --help                Show this message and exit.
```

Logging and telemetry are set up once here, at the group entrypoint, so **every**
command exports spans and metrics — not just `serve`. `--telemetry-console` is
therefore global too, and works for any command:

```bash
lithos --telemetry-console reconcile
```

## Commands

### `serve` — Start the MCP server

```bash
# stdio transport (default — for MCP clients like Claude Desktop)
lithos serve

# HTTP transport — serves both /mcp (StreamableHTTP) and /sse (legacy SSE)
# on the same port, for network clients like Agent Zero and Hermes Agent.
lithos serve --transport http --host 0.0.0.0 --port 8765

# Disable file watcher
lithos serve --no-watch
```

| Option | Default | Description |
|---|---|---|
| `-t, --transport` | `stdio` | Transport type: `stdio` or `http` (`http` serves both `/mcp` and `/sse`) |
| `--host` | `127.0.0.1` | Host for the HTTP transport |
| `-p, --port` | `8765` | Port for the HTTP transport |
| `--watch / --no-watch` | watch enabled | Watch for file changes |

> **Metrics:** Lithos exports metrics via OTLP push to a configured OTEL collector — there is no `/metrics` scrape endpoint on the process itself. See the "Telemetry & Observability" section in the main README for the full push→collector→Prometheus data flow, or use the global `--telemetry-console` option when no collector is available.

### `search` — Search the knowledge base

```bash
# Full-text search (default)
lithos search "agent coordination"

# Semantic / vector search
lithos search --semantic "how do agents share findings"

# Limit number of results
lithos search -n 10 "knowledge graph"
```

| Option | Default | Description |
|---|---|---|
| `--semantic / --fulltext` | fulltext | Search mode |
| `-n, --limit` | `5` | Number of results to return |

### `stats` — Show knowledge base statistics

```bash
lithos stats
lithos --data-dir ./docker/data stats
```

Outputs document count, search chunks, graph nodes/edges, tags, registered agents, active tasks, and open claims.

### `reindex` — Rebuild search indices

```bash
# Incremental reindex
lithos reindex

# Wipe and rebuild from scratch
lithos reindex --clear
```

Use this after manually editing or adding Markdown files outside of the MCP interface.

### `recalibrate-salience` — One-time salience backfill

```bash
# Preview: show the distribution and how many rows would be lifted
lithos recalibrate-salience --dry-run

# Lift decay-collapsed rows up to the floor (config lcma.salience_floor)
lithos recalibrate-salience

# Override the floor explicitly
lithos recalibrate-salience --floor 0.3
```

Corrects the historical salience collapse (task `e7d8ef60`) where a floor-less daily
decay drove most nodes to ~0. Lifts every node below the floor up to it, **except** nodes
carrying explicit negative feedback (misleading / chronically ignored), which stay below
deliberately. Idempotent — safe to re-run. Run it **once on staging first, then prod**,
with the server stopped is not required (it only touches `stats.db`), but the operator
should confirm the reported before/after distribution. Going forward the daily enrich
sweep holds the floor, so this should not need re-running.

### `validate` — Check knowledge base integrity

```bash
# Report issues only
lithos validate

# Report and attempt auto-repair
lithos validate --fix
```

Checks for missing IDs, missing titles, missing authors, broken `[[wiki-links]]`, and ambiguous link targets.

### `extract-entities` — Re-extract entity frontmatter

```bash
# Preview what would change
lithos extract-entities --dry-run

# Honour the curation contract (default): only documents with no entities
# or a stale entities_extractor marker are re-extracted
lithos extract-entities

# Bootstrap a corpus written before extractor provenance existed:
# re-extract every document, including ones with markerless entities
lithos extract-entities --force --dry-run
lithos extract-entities --force
```

Unlike `reconcile`, this command **mutates Markdown source files**: it replaces
each document's `entities` list with the current extractor's output and stamps
`entities_extractor` provenance. Entities without a marker are treated as
agent-curated and skipped unless `--force` is given. Run `lithos reconcile`
afterwards to refresh derived views.

> **NER model:** entity extraction uses the spaCy `en_core_web_sm` model. It is
> **not** a PyPI dependency (the model is distributed as a direct-URL wheel,
> which PyPI rejects), so lithos **downloads it on first use** and caches it.
> If the download fails (e.g. offline), extraction transparently falls back to
> high-precision heuristic extraction — NER simply runs in degraded mode.
> To pre-install it (or for fully offline use), run
> `python -m spacy download en_core_web_sm`. The Docker image bakes it in at
> build time.

## Specifying a Data Directory

All commands accept `--data-dir` (or `-d`) to point at a non-default data location:

```bash
lithos --data-dir /path/to/data stats
lithos -d ./docker/data search "my query"
```

You can also use a YAML config file:

```bash
lithos --config lithos.yaml serve
```

## Running Without Installing

```bash
# As a Python module
python -m lithos.cli --help

# Directly from source
python src/lithos/cli.py --help
```

## Inside the Docker Container

The `lithos` command is available inside the running container:

```bash
docker compose exec lithos lithos stats
docker compose exec lithos lithos search "my query"
docker compose exec lithos lithos validate
```

## Getting Help

Every command has a `--help` flag:

```bash
lithos --help
lithos serve --help
lithos search --help
lithos reindex --help
lithos validate --help
lithos stats --help
```
