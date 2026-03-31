"""Lithos CLI - Command-line interface."""

import asyncio
import logging
import sys
from pathlib import Path

import click

from lithos.config import LithosConfig, load_config, set_config


@click.group()
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True, path_type=Path),
    help="Path to config file (YAML)",
)
@click.option(
    "--data-dir",
    "-d",
    type=click.Path(path_type=Path),
    help="Data directory path",
)
@click.pass_context
def cli(ctx: click.Context, config: Path | None, data_dir: Path | None) -> None:
    """Lithos - Local shared knowledge base for AI agents."""
    ctx.ensure_object(dict)

    # Load configuration (load_config reads LITHOS_* env vars)
    cfg = load_config(str(config)) if config else load_config()

    # Override data directory if specified
    if data_dir:
        cfg.storage.data_dir = data_dir

    set_config(cfg)
    ctx.obj["config"] = cfg


@cli.command()
@click.option(
    "--transport",
    "-t",
    type=click.Choice(["stdio", "sse"]),
    default="stdio",
    help="Transport type (default: stdio)",
)
@click.option(
    "--host",
    default="127.0.0.1",
    help="Host for SSE transport (default: 127.0.0.1)",
)
@click.option(
    "--port",
    "-p",
    type=int,
    default=8765,
    help="Port for SSE transport (default: 8765)",
)
@click.option(
    "--watch/--no-watch",
    default=True,
    help="Watch for file changes (default: enabled)",
)
@click.pass_context
def serve(
    ctx: click.Context,
    transport: str,
    host: str,
    port: int,
    watch: bool,
) -> None:
    """Start the Lithos MCP server."""
    from lithos.server import create_server
    from lithos.telemetry import setup_telemetry, shutdown_telemetry

    config: LithosConfig = ctx.obj["config"]
    server = create_server(config)
    log_fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    log_datefmt = "%Y-%m-%dT%H:%M:%S%z"

    async def run_server() -> None:
        # Initialize server
        click.echo("Initializing Lithos...")
        await server.initialize()

        # Start file watcher if enabled
        if watch:
            click.echo("Starting file watcher...")
            server.start_file_watcher()

        click.echo(f"Starting MCP server ({transport} transport)...")

        if transport == "stdio":
            # Run with stdio transport
            await server.mcp.run_stdio_async(show_banner=False)
        else:
            # Run with SSE transport using run_http_async
            click.echo(f"Listening on http://{host}:{port}")
            await server.mcp.run_http_async(
                transport="sse",
                host=host,
                port=port,
                path="/sse",
                show_banner=False,
                uvicorn_config={
                    "log_config": {
                        "version": 1,
                        "disable_existing_loggers": False,
                        "formatters": {
                            "default": {"fmt": log_fmt, "datefmt": log_datefmt},
                            "access": {"fmt": log_fmt, "datefmt": log_datefmt},
                        },
                        "handlers": {
                            "default": {
                                "formatter": "default",
                                "class": "logging.StreamHandler",
                                "stream": "ext://sys.stderr",
                            },
                            "access": {
                                "formatter": "access",
                                "class": "logging.StreamHandler",
                                "stream": "ext://sys.stdout",
                            },
                        },
                        "loggers": {
                            "uvicorn": {
                                "handlers": ["default"],
                                "level": "INFO",
                                "propagate": False,
                            },
                            "uvicorn.error": {"level": "INFO", "propagate": False},
                            "uvicorn.access": {
                                "handlers": ["access"],
                                "level": "INFO",
                                "propagate": False,
                            },
                        },
                    },
                },
            )

    try:
        logging.basicConfig(level=logging.INFO, format=log_fmt, datefmt=log_datefmt)
        setup_telemetry(config)
        asyncio.run(run_server())
    except KeyboardInterrupt:
        click.echo("\nShutting down...")
        server.stop_file_watcher()
    finally:
        shutdown_telemetry()


@cli.command()
@click.option(
    "--clear/--no-clear",
    default=False,
    help="Clear indices before rebuilding (default: no)",
)
@click.pass_context
def reindex(ctx: click.Context, clear: bool) -> None:
    """Rebuild search indices from knowledge files."""
    from lithos.graph import KnowledgeGraph
    from lithos.knowledge import KnowledgeManager
    from lithos.search import SearchEngine

    config: LithosConfig = ctx.obj["config"]
    config.ensure_directories()

    knowledge = KnowledgeManager()
    search = SearchEngine(config)
    graph = KnowledgeGraph(config)

    async def do_reindex() -> None:
        if clear:
            click.echo("Clearing existing indices...")
            search.clear_all()
            graph.clear()

        knowledge_path = config.storage.knowledge_path
        files = list(knowledge_path.rglob("*.md"))

        click.echo(f"Found {len(files)} markdown files")

        indexed = 0
        errors = 0

        with click.progressbar(files, label="Indexing") as bar:
            for file_path in bar:
                try:
                    relative_path = file_path.relative_to(knowledge_path)
                    doc, _ = await knowledge.read(path=str(relative_path))
                    search.index_document(doc)
                    graph.add_document(doc)
                    indexed += 1
                except Exception as e:
                    errors += 1
                    click.echo(f"\nError indexing {file_path}: {e}", err=True)

        # Save graph cache
        graph.save_cache()

        click.echo(f"\nIndexed {indexed} documents")
        if errors:
            click.echo(f"Errors: {errors}", err=True)

        # Show stats
        stats = search.get_stats()
        click.echo(f"Total chunks: {stats.get('chunks', 0)}")
        click.echo(f"Graph nodes: {graph.node_count()}")
        click.echo(f"Graph edges: {graph.edge_count()}")

    asyncio.run(do_reindex())


@cli.command()
@click.option(
    "--fix/--no-fix",
    default=False,
    help="Attempt to fix issues (default: no)",
)
@click.pass_context
def validate(ctx: click.Context, fix: bool) -> None:
    """Validate knowledge base integrity."""
    from lithos.graph import KnowledgeGraph
    from lithos.knowledge import KnowledgeManager

    config: LithosConfig = ctx.obj["config"]
    config.ensure_directories()

    knowledge = KnowledgeManager()
    graph = KnowledgeGraph(config)

    async def do_validate() -> None:
        knowledge_path = config.storage.knowledge_path
        files = list(knowledge_path.rglob("*.md"))

        click.echo(f"Validating {len(files)} files...\n")

        issues: list[tuple[str, str, str]] = []  # (file, issue_type, message)

        # Check each file
        for file_path in files:
            relative_path = file_path.relative_to(knowledge_path)

            try:
                doc, _ = await knowledge.read(path=str(relative_path))

                # Check for missing ID
                if not doc.id:
                    issues.append((str(relative_path), "missing_id", "No UUID in frontmatter"))

                # Check for missing title
                if not doc.title:
                    issues.append((str(relative_path), "missing_title", "No title in frontmatter"))

                # Check for missing author
                if not doc.metadata.author:
                    issues.append(
                        (str(relative_path), "missing_author", "No author in frontmatter")
                    )

                # Add to graph for link checking
                graph.add_document(doc)

            except Exception as e:
                issues.append((str(relative_path), "parse_error", str(e)))

        # Check for broken links
        broken_links = graph.get_broken_links()
        for _source_id, source_title, target in broken_links:
            issues.append((source_title, "broken_link", f"Link to '{target}' not found"))

        # Check for ambiguous links
        ambiguous = graph.get_ambiguous_links()
        for filename, paths in ambiguous:
            issues.append((filename, "ambiguous", f"Multiple files match: {', '.join(paths)}"))

        # Report issues
        if issues:
            click.echo("Issues found:\n")

            # Group by type
            by_type: dict[str, list[tuple[str, str]]] = {}
            for file, issue_type, message in issues:
                if issue_type not in by_type:
                    by_type[issue_type] = []
                by_type[issue_type].append((file, message))

            for issue_type, items in sorted(by_type.items()):
                click.echo(f"  {issue_type.upper()} ({len(items)}):")
                for file, message in items[:10]:  # Show first 10
                    click.echo(f"    - {file}: {message}")
                if len(items) > 10:
                    click.echo(f"    ... and {len(items) - 10} more")
                click.echo()

            click.echo(f"Total issues: {len(issues)}")

            if fix:
                click.echo("\nAttempting fixes...")
                fixed = await _fix_issues(knowledge, issues)
                click.echo(f"Fixed {fixed} issues")
        else:
            click.echo("No issues found")

    async def _fix_issues(
        knowledge: KnowledgeManager,
        issues: list[tuple[str, str, str]],
    ) -> int:
        """Attempt to fix issues."""
        fixed = 0

        for file, issue_type, _ in issues:
            if issue_type == "missing_id":
                # Add UUID to file
                try:
                    doc, _ = await knowledge.read(path=file)
                    # Re-save will add ID if missing
                    await knowledge.update(
                        id=doc.id,
                        agent="lithos-cli",
                    )
                    fixed += 1
                except Exception:
                    pass

        return fixed

    asyncio.run(do_validate())


@cli.command()
@click.pass_context
def stats(ctx: click.Context) -> None:
    """Show knowledge base statistics."""
    from lithos.coordination import CoordinationService
    from lithos.graph import KnowledgeGraph
    from lithos.knowledge import KnowledgeManager
    from lithos.search import SearchEngine

    config: LithosConfig = ctx.obj["config"]

    knowledge = KnowledgeManager()
    search = SearchEngine(config)
    graph = KnowledgeGraph(config)
    coordination = CoordinationService(config)

    async def show_stats() -> None:
        # Initialize coordination DB
        await coordination.initialize()

        # Load graph cache
        graph.load_cache()

        # Get counts
        _, total_docs = await knowledge.list_all(limit=0)
        search_stats = search.get_stats()
        coord_stats = await coordination.get_stats()
        tags = await knowledge.get_all_tags()

        click.echo("Lithos Statistics")
        click.echo("=" * 40)
        click.echo(f"Documents:     {total_docs}")
        click.echo(f"Search chunks: {search_stats.get('chunks', 0)}")
        click.echo(f"Graph nodes:   {graph.node_count()}")
        click.echo(f"Graph edges:   {graph.edge_count()}")
        click.echo(f"Tags:          {len(tags)}")
        click.echo(f"Agents:        {coord_stats.get('agents', 0)}")
        click.echo(f"Active tasks:  {coord_stats.get('active_tasks', 0)}")
        click.echo(f"Open claims:   {coord_stats.get('open_claims', 0)}")
        click.echo()
        click.echo(f"Data directory: {config.storage.data_dir}")

    asyncio.run(show_stats())


@cli.command()
@click.argument("query")
@click.option(
    "--semantic/--fulltext",
    default=False,
    help="Use semantic search (default: fulltext)",
)
@click.option(
    "--limit",
    "-n",
    type=int,
    default=5,
    help="Number of results (default: 5)",
)
@click.pass_context
def search(ctx: click.Context, query: str, semantic: bool, limit: int) -> None:
    """Search the knowledge base."""
    from lithos.search import SearchEngine

    config: LithosConfig = ctx.obj["config"]
    engine = SearchEngine(config)

    if semantic:
        click.echo(f"Semantic search: {query}\n")
        results = engine.semantic_search(query, limit=limit)
        for i, r in enumerate(results, 1):
            click.echo(f"{i}. {r.title} (similarity: {r.similarity:.2f})")
            click.echo(f"   Path: {r.path}")
            click.echo(f"   {r.snippet[:100]}...\n")
    else:
        click.echo(f"Full-text search: {query}\n")
        results = engine.full_text_search(query, limit=limit)
        for i, r in enumerate(results, 1):
            click.echo(f"{i}. {r.title} (score: {r.score:.2f})")
            click.echo(f"   Path: {r.path}")
            click.echo(f"   {r.snippet[:100]}...\n")

    if not results:
        click.echo("No results found.")


@cli.command()
@click.option(
    "--scope",
    "-s",
    type=click.Choice(["all", "indices", "graph", "provenance_projection"]),
    default="all",
    help="Reconcile scope (default: all)",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=False,
    help="Compute diffs without applying any repairs (default: no)",
)
@click.option(
    "--json-output/--no-json-output",
    default=False,
    help="Output result as JSON (default: no)",
)
@click.pass_context
def reconcile(ctx: click.Context, scope: str, dry_run: bool, json_output: bool) -> None:
    """Reconcile derived projections (indices, graph) against the markdown corpus.

    This is an operator/admin command.  It repairs drift in search indices and the
    wiki-link graph cache without ever mutating markdown source files.
    """
    import json

    from lithos.reconcile import reconcile as _reconcile

    config: LithosConfig = ctx.obj["config"]
    config.ensure_directories()

    async def run() -> None:
        result = await _reconcile(
            scope=scope,  # type: ignore[arg-type]
            dry_run=dry_run,
            config=config,
        )

        if json_output:
            click.echo(json.dumps(result, indent=2))
            return

        dry_label = " [DRY RUN]" if dry_run else ""
        click.echo(f"Reconcile{dry_label}: scope={result['scope']}")
        click.echo(f"  status:   {result['status']}")
        click.echo(f"  supported:{' yes' if result['supported'] else ' no'}")
        s = result["summary"]
        click.echo(
            f"  summary:  scanned={s['scanned']} repaired={s['repaired']}"
            f" failed={s['failed']} skipped={s['skipped']}"
        )

        if result["actions"]:
            click.echo("  actions:")
            for action in result["actions"]:
                click.echo(f"    - {action}")

        if result["failures"]:
            click.echo("  failures:", err=True)
            for failure in result["failures"]:
                click.echo(f"    - {failure}", err=True)

        non_ok = result["status"] not in ("ok", "noop")
        sys.exit(1 if (result["supported"] is not False and non_ok) else 0)

    asyncio.run(run())


@cli.group()
def inspect() -> None:
    """Inspect agent state, tasks, documents, and backend health."""


@inspect.command(name="health")
@click.pass_context
def inspect_health(ctx: click.Context) -> None:
    """Show backend health (Tantivy, ChromaDB)."""
    from lithos.search import SearchEngine

    config: LithosConfig = ctx.obj["config"]
    engine = SearchEngine(config)

    status: dict[str, str] = {}

    try:
        _ = engine.tantivy.index  # triggers open_or_create if needed
        status["tantivy"] = "ok"
    except Exception as exc:
        status["tantivy"] = f"unavailable: {exc}"

    try:
        _ = engine.chroma.collection.count()
        status["chroma"] = "ok"
    except Exception as exc:
        status["chroma"] = f"unavailable: {exc}"

    click.echo("Backend health")
    click.echo("=" * 30)
    all_ok = True
    for backend, state in status.items():
        icon = "✓" if state == "ok" else "✗"
        click.echo(f"  {icon}  {backend}: {state}")
        if state != "ok":
            all_ok = False

    sys.exit(0 if all_ok else 1)


@inspect.command(name="agents")
@click.pass_context
def inspect_agents(ctx: click.Context) -> None:
    """List registered agents and their last-seen time."""
    from lithos.coordination import CoordinationService

    config: LithosConfig = ctx.obj["config"]

    async def run() -> None:
        coord = CoordinationService(config)
        await coord.initialize()
        agents = await coord.list_agents()

        click.echo(f"Agents ({len(agents)} total)\n{'=' * 50}")
        if not agents:
            click.echo("  No agents registered.")
            return

        for agent in agents:
            last_seen = (
                agent.last_seen_at.strftime("%Y-%m-%d %H:%M:%S") if agent.last_seen_at else "never"
            )
            click.echo(f"  {agent.id}")
            click.echo(f"    name:      {agent.name or '—'}")
            click.echo(f"    type:      {agent.type or '—'}")
            click.echo(f"    last seen: {last_seen}")

    asyncio.run(run())


@inspect.command(name="tasks")
@click.option(
    "--all", "show_all", is_flag=True, default=False, help="Show all tasks, not just open ones"
)
@click.pass_context
def inspect_tasks(ctx: click.Context, show_all: bool) -> None:
    """List coordination tasks (open by default)."""
    from lithos.coordination import CoordinationService

    config: LithosConfig = ctx.obj["config"]

    async def run() -> None:
        coord = CoordinationService(config)
        await coord.initialize()

        task_statuses = await coord.get_task_status(include_all=show_all)

        label = "all" if show_all else "open"
        click.echo(f"Tasks ({len(task_statuses)} {label})\n{'=' * 50}")
        if not task_statuses:
            click.echo("  No tasks found.")
            return

        for ts in task_statuses:
            click.echo(f"  [{ts.status.upper()}] {ts.title}")
            click.echo(f"    id:     {ts.id}")
            if ts.claims:
                for claim in ts.claims:
                    expires = claim.expires_at.strftime("%H:%M:%S")
                    click.echo(f"    claim:  {claim.agent} → {claim.aspect} (expires {expires})")

    asyncio.run(run())


@inspect.command(name="doc")
@click.argument("identifier")
@click.option("--content/--no-content", default=False, help="Show full content (default: no)")
@click.pass_context
def inspect_doc(ctx: click.Context, identifier: str, content: bool) -> None:
    """Inspect a knowledge document by ID or path."""
    from lithos.knowledge import KnowledgeManager

    async def run() -> None:
        knowledge = KnowledgeManager()

        # Try by ID first, then by path
        try:
            if len(identifier) == 36 and identifier.count("-") == 4:
                doc, truncated = await knowledge.read(id=identifier)
            else:
                doc, truncated = await knowledge.read(path=identifier)
        except Exception as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)

        click.echo(f"Document: {doc.title}")
        click.echo("=" * 50)
        click.echo(f"  id:      {doc.id}")
        click.echo(f"  path:    {doc.path}")
        click.echo(f"  author:  {doc.metadata.author or '—'}")
        click.echo(f"  tags:    {', '.join(doc.metadata.tags) if doc.metadata.tags else '—'}")
        created = (
            doc.metadata.created_at.strftime("%Y-%m-%d %H:%M") if doc.metadata.created_at else "—"
        )
        updated = (
            doc.metadata.updated_at.strftime("%Y-%m-%d %H:%M") if doc.metadata.updated_at else "—"
        )
        click.echo(f"  created: {created}")
        click.echo(f"  updated: {updated}")
        click.echo(f"  size:    {len(doc.content)} chars")

        if content:
            click.echo("\nContent:")
            click.echo("-" * 50)
            click.echo(doc.content)
            if truncated:
                click.echo("\n[content truncated]")

    asyncio.run(run())


@cli.command()
@click.option(
    "--agent",
    "-a",
    default=None,
    help="Filter entries by agent ID",
)
@click.option(
    "--since",
    "-s",
    default=None,
    help="Only show entries after this ISO-8601 timestamp (e.g. 2026-01-01T00:00:00)",
)
@click.option(
    "--limit",
    "-n",
    type=int,
    default=50,
    help="Maximum number of entries to show (default: 50)",
)
@click.option(
    "--doc",
    default=None,
    help="Filter entries by document ID",
)
@click.pass_context
def audit(
    ctx: click.Context, agent: str | None, since: str | None, limit: int, doc: str | None
) -> None:
    """Show the read-access audit log.

    Displays documents that have been read or returned in search results,
    with the agent that triggered each access.
    """
    import asyncio

    from lithos.coordination import CoordinationService

    config: LithosConfig = ctx.obj["config"]

    async def run() -> None:
        service = CoordinationService(config)
        await service.initialize()
        entries = await service.get_audit_log(agent_id=agent, after=since, limit=limit, doc_id=doc)

        if not entries:
            click.echo("No audit log entries found.")
            return

        click.echo(f"{'ID':>6}  {'TIMESTAMP':<27}  {'AGENT':<20}  {'OP':<14}  DOC ID")
        click.echo("-" * 95)
        for e in entries:
            ts = e.timestamp.isoformat() if e.timestamp else "unknown"
            click.echo(f"{e.id:>6}  {ts:<27}  {e.agent_id:<20}  {e.operation:<14}  {e.doc_id}")

    asyncio.run(run())


def main() -> None:
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
