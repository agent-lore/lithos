# Lithos

**Shared memory for AI agents.**

A local, privacy-first knowledge base that enables heterogeneous AI agents to share knowledge and coordinate work.

## The Problem

When agents cannot share what they know, every agent starts from zero. Work is duplicated, discoveries are lost, and coordination breaks down. Lithos solves this by providing a **persistent, shared knowledge layer** that compounds in value over time.

## What It Is

Lithos is an MCP server that provides a shared knowledge store for AI agents running on your local infrastructure. Knowledge is stored as human-readable Markdown files (compatible with Obsidian) while providing fast full-text and semantic search for agents.

## Who It's For

Lithos is the **Knowledge Layer** for teams running AI agents in production.

Just as Alation coined the term "Knowledge Layer" for enterprise data governance, Lithos provides the equivalent for AI agent systems: a structured, searchable, shared memory that **compounds in value** the more it is used. Each agent interaction enriches the knowledge base, making every subsequent agent smarter and faster.

- Teams running multiple AI agents (Agent Zero, OpenClaw, Claude Code, custom agents)
- Developers who want agents to share discoveries and avoid duplicate work
- Anyone who needs agent knowledge to be inspectable and version-controlled

## Key Features

- **📁 Markdown-first**: All knowledge stored as Obsidian-compatible `.md` files
- **🔍 Fast search**: Tantivy full-text + ChromaDB semantic search
- **🕸️ Knowledge graph**: NetworkX-powered relationships via `[[wiki-links]]`
- **🤝 Multi-agent coordination**: Task claiming, findings sharing, status tracking
- **🧠 Research cache**: One-call freshness check so agents skip redundant research — returns hit/miss/stale with update guidance
- **🔗 URL deduplication**: Automatic detection and prevention of duplicate notes from the same source URL
- **🧬 Provenance tracking**: Declare which notes a synthesis is derived from and query lineage across the knowledge base
- **🔌 MCP interface**: Works with any MCP-compatible agent or tool
- **🏠 Local & private**: No cloud dependencies, you own your data

## Quickstart

### Agent Zero

Agent Zero running inside docker on the same machine running lithos

```json
{
  "mcpServers": {
    "lithos": {
      "url": "http://host.docker.internal:8765/sse"
    }
  }
}
```

### OpenClaw

Update `mcporter.json` Probably in `~/.openclaw/workspace/config/mcporter.json`

Use `localhost` if running on the same machine as OpenClaw, otherwise the name or IP address of the server.

```json
{
  "mcpServers": {
    "lithos": {
      "baseUrl": "http://<your hostname>:8765/sse"
    }
  },
  "imports": []
}
```

### Claude code

```bash
claude mcp add --transport sse lithos http://localhost:8765/sse
```

## Documentation

- [CLI Reference](docs/cli.md) — installing and using the `lithos` command-line tool
- [Specification](docs/SPECIFICATION.md) — full technical specification
- [LCMA Design](docs/lcma-design.md) — design notes

## Tech Stack

| Component | Technology |
|-----------|------------|
| Storage | Markdown + YAML frontmatter |
| Full-text search | Tantivy |
| Semantic search | ChromaDB + sentence-transformers |
| Knowledge graph | NetworkX |
| Agent interface | MCP (FastMCP) |
| File sync | watchdog |

## Development Commands

```bash
# Install dependencies (uses uv)
uv sync --extra dev

# Run unit tests
uv run --extra dev pytest -m "not integration" tests/ -q

# Run integration tests
uv run --extra dev pytest -m integration tests/ -q

# Run all tests with coverage
uv run --extra dev pytest tests/ --cov=lithos --cov-report=xml

# Lint
uv run --extra dev ruff check .

# Format check
uv run --extra dev ruff format --check src/ tests/

# Type check
uv run --extra dev pyright src/

# Auto-fix lint + format
uv run --extra dev ruff check --fix . && uv run --extra dev ruff format src/ tests/

# Start server (stdio)
uv run lithos serve

# Start server (SSE)
uv run lithos serve --transport sse --port 8765

# Docker
cd docker && docker compose up -d --build

# run pointing at data dir
LITHOS_DATA_PATH="<DATA DIR PATH>" docker compose up -d --build

# stop
cd docker && docker compose down
```

## Docker: running multiple environments

Lithos ships with `docker/run.sh`, a thin wrapper around `docker compose`
that drives each environment from its own gitignored `.env.<name>` file
and a distinct compose project name (`-p lithos-<name>`). This lets you
run `prod`, `staging`, and `fuzz` side-by-side on one host without
container name, port, or volume collisions.

### Set up env files

Create one file per environment under `docker/`:

`docker/.env.prod`
```bash
LITHOS_ENVIRONMENT=production
LITHOS_DATA_PATH=/path/to/lithos/data
LITHOS_HOST_PORT=8765
LITHOS_CONTAINER_NAME=lithos
```

`docker/.env.staging`
```bash
LITHOS_ENVIRONMENT=staging
LITHOS_DATA_PATH=/path/to/lithos/data-staging
LITHOS_HOST_PORT=8766
LITHOS_CONTAINER_NAME=lithos-staging
```

`docker/.env.fuzz`
```bash
LITHOS_ENVIRONMENT=fuzz
LITHOS_DATA_PATH=/path/to/lithos/data-fuzz
LITHOS_HOST_PORT=8767
LITHOS_CONTAINER_NAME=lithos-fuzz
```

`LITHOS_ENVIRONMENT` becomes the OTEL `deployment.environment` resource
attribute, so metrics, traces, and logs are labelled per environment in
your observability stack.

### Use the launcher

```bash
cd docker

./run.sh prod                 # build & start production (default action = up)
./run.sh staging up           # same, explicit
./run.sh fuzz logs            # follow container logs
./run.sh staging status       # show running containers for this stack
./run.sh prod down            # stop & remove the stack
./run.sh fuzz restart         # down + up
```

Each environment gets its own container (`lithos`, `lithos-staging`,
`lithos-fuzz`), its own host port, and its own data volume, so they can
all run concurrently. Running `./run.sh` with no arguments prints usage.

## Telemetry & Observability

Lithos emits OpenTelemetry metrics, traces, and logs when telemetry is enabled.
The **only** supported export path is **OTLP/HTTP push to a collector** — there
is no `/metrics` scrape endpoint on the Lithos process itself (see closed
issue [#164](https://github.com/agent-lore/lithos/issues/164) for the
rationale).

### How metrics reach your dashboards

```
  Lithos process
       │  OTLP/HTTP  (push every export_interval_ms, default 30 s)
       ▼
  OTEL Collector   ← lithos-observability/otel-collector/config.yml
       │  Prometheus exporter on :8889
       ▼
  Prometheus       ← lithos-observability/prometheus/prometheus.yml
       │
       ▼
  Grafana
```

Traces fan out to Tempo, logs to Loki, via the same collector.

### Configuration

```yaml
telemetry:
  enabled: false               # master switch
  endpoint: null               # OTLP base URL, e.g. http://otel-collector:4318
  console_fallback: false      # print spans/metrics to stdout when no endpoint
  service_name: lithos
  environment: null            # becomes OTEL deployment.environment
  export_interval_ms: 30000
```

Environment variables override `endpoint` per signal when needed:
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`,
`OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`.

### Local debugging without a collector

Pass `--telemetry-console` to `lithos serve` to route metrics and spans to
stdout via console exporters. This is equivalent to setting
`telemetry.enabled=true` + `telemetry.console_fallback=true` in config, and is
the shortest path to "is my instrumentation even firing?" when no collector is
running.

```bash
lithos --data-dir ./data serve --telemetry-console
```

### Running the full observability stack locally

See `lithos-observability/` for a one-command Docker Compose stack (OTEL
Collector + Prometheus + Grafana + Tempo + Loki). Point Lithos at it with:

```bash
LITHOS_TELEMETRY__ENABLED=true \
LITHOS_TELEMETRY__ENDPOINT=http://localhost:4318 \
lithos serve
```