"""Exogram CLI - Command-line interface."""

import asyncio
from pathlib import Path

import click

from exogram.config import ExogramConfig, load_config, set_config


@click.group()
@click.option(
    "--config", "-c",
    type=click.Path(exists=True, path_type=Path),
    help="Path to config file (YAML)",
)
@click.option(
    "--data-dir", "-d",
    type=click.Path(path_type=Path),
    help="Data directory path",
)
@click.pass_context
def cli(ctx: click.Context, config: Path | None, data_dir: Path | None) -> None:
    """Exogram - Local shared knowledge base for AI agents."""
    ctx.ensure_object(dict)

    # Load configuration
    if config:
        cfg = load_config(config)
    else:
        cfg = ExogramConfig()

    # Override data directory if specified
    if data_dir:
        cfg.storage.data_dir = data_dir

    set_config(cfg)
    ctx.obj["config"] = cfg


@cli.command()
@click.option(
    "--transport", "-t",
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
    "--port", "-p",
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
    """Start the Exogram MCP server."""
    from exogram.server import create_server

    config: ExogramConfig = ctx.obj["config"]
    server = create_server(config)

    async def run_server() -> None:
        # Initialize server
        click.echo("Initializing Exogram...")
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
            )

    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        click.echo("\nShutting down...")
        server.stop_file_watcher()


@cli.command()
@click.option(
    "--clear/--no-clear",
    default=False,
    help="Clear indices before rebuilding (default: no)",
)
@click.pass_context
def reindex(ctx: click.Context, clear: bool) -> None:
    """Rebuild search indices from knowledge files."""
    from exogram.graph import KnowledgeGraph
    from exogram.knowledge import KnowledgeManager
    from exogram.search import SearchEngine

    config: ExogramConfig = ctx.obj["config"]
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
    from exogram.graph import KnowledgeGraph
    from exogram.knowledge import KnowledgeManager

    config: ExogramConfig = ctx.obj["config"]
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
                    issues.append((str(relative_path), "missing_author", "No author in frontmatter"))

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
                        agent="exogram-cli",
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
    from exogram.coordination import CoordinationService
    from exogram.graph import KnowledgeGraph
    from exogram.knowledge import KnowledgeManager
    from exogram.search import SearchEngine

    config: ExogramConfig = ctx.obj["config"]

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

        click.echo("Exogram Statistics")
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
    "--limit", "-n",
    type=int,
    default=5,
    help="Number of results (default: 5)",
)
@click.pass_context
def search(ctx: click.Context, query: str, semantic: bool, limit: int) -> None:
    """Search the knowledge base."""
    from exogram.search import SearchEngine

    config: ExogramConfig = ctx.obj["config"]
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


def main() -> None:
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
