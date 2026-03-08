"""CLI contract tests for stable output shape."""

import asyncio
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lithos.cli import cli
from lithos.config import LithosConfig, StorageConfig, set_config
from lithos.coordination import CoordinationService
from lithos.knowledge import KnowledgeManager

pytestmark = pytest.mark.integration


class TestCLIContracts:
    """Validate current CLI command output contracts."""

    def test_stats_output_shape(self, temp_dir):
        runner = CliRunner()
        result = runner.invoke(cli, ["--data-dir", str(temp_dir), "stats"])

        assert result.exit_code == 0, result.output
        assert "Lithos Statistics" in result.output
        assert "Documents:" in result.output
        assert "Search chunks:" in result.output
        assert "Graph nodes:" in result.output
        assert "Graph edges:" in result.output
        assert "Data directory:" in result.output

    def test_reindex_and_search_output_shape(self, temp_dir):
        config = LithosConfig(storage=StorageConfig(data_dir=temp_dir))
        config.ensure_directories()
        set_config(config)

        knowledge = KnowledgeManager()

        async def _seed() -> None:
            await knowledge.create(
                title="CLI Contract Seed",
                content="ContractTerm appears here for full-text verification.",
                agent="cli-test",
            )

        asyncio.run(_seed())

        runner = CliRunner()
        reindex = runner.invoke(cli, ["--data-dir", str(temp_dir), "reindex"])
        assert reindex.exit_code == 0, reindex.output
        assert "Found 1 markdown files" in reindex.output
        assert "Indexed 1 documents" in reindex.output
        assert "Total chunks:" in reindex.output

        search = runner.invoke(
            cli,
            ["--data-dir", str(temp_dir), "search", "ContractTerm", "--fulltext", "--limit", "3"],
        )
        assert search.exit_code == 0, search.output
        assert "Full-text search: ContractTerm" in search.output
        assert "1. CLI Contract Seed (score:" in search.output
        assert "Path:" in search.output

    def test_inspect_doc_output_shape(self, temp_dir):
        config = LithosConfig(storage=StorageConfig(data_dir=temp_dir))
        config.ensure_directories()
        set_config(config)
        knowledge = KnowledgeManager()

        async def _seed():
            return (
                await knowledge.create(
                    title="Inspect Contract Doc",
                    content="Inspect output shape contract.",
                    agent="cli-test",
                    tags=["contract", "inspect"],
                )
            ).document

        doc = asyncio.run(_seed())

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--data-dir", str(temp_dir), "inspect", "doc", doc.id],
        )

        assert result.exit_code == 0, result.output
        assert "Document: Inspect Contract Doc" in result.output
        assert "id:" in result.output
        assert "path:" in result.output
        assert "author:" in result.output
        assert "tags:" in result.output
        assert "size:" in result.output

    def test_validate_reports_and_fixes(self, temp_dir):
        config = LithosConfig(storage=StorageConfig(data_dir=temp_dir))
        config.ensure_directories()
        set_config(config)
        knowledge = KnowledgeManager()

        async def _seed():
            await knowledge.create(
                title="Validate Source Doc",
                content="This links to [[missing-target-doc]].",
                agent="cli-test",
            )

        asyncio.run(_seed())

        # Add malformed file to trigger parse_error bucket.
        bad_file = config.storage.knowledge_path / "bad-frontmatter.md"
        bad_file.write_text("---\n: broken yaml\n---\nBody")

        runner = CliRunner()
        result = runner.invoke(cli, ["--data-dir", str(temp_dir), "validate"])
        assert result.exit_code == 0, result.output
        assert "Issues found:" in result.output
        assert "BROKEN_LINK" in result.output
        assert "PARSE_ERROR" in result.output
        assert "Total issues:" in result.output

        fixed = runner.invoke(cli, ["--data-dir", str(temp_dir), "validate", "--fix"])
        assert fixed.exit_code == 0, fixed.output
        assert "Attempting fixes..." in fixed.output
        assert "Fixed " in fixed.output

    def test_search_semantic_no_results_output_shape(self, temp_dir):
        config = LithosConfig(storage=StorageConfig(data_dir=temp_dir))
        config.ensure_directories()
        set_config(config)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--data-dir",
                str(temp_dir),
                "search",
                "no-such-semantic-needle",
                "--semantic",
                "--limit",
                "2",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Semantic search: no-such-semantic-needle" in result.output
        assert "No results found." in result.output

    def test_inspect_agents_and_tasks_output_shape(self, temp_dir):
        config = LithosConfig(storage=StorageConfig(data_dir=temp_dir))
        config.ensure_directories()
        set_config(config)

        async def _seed():
            coordination = CoordinationService(config)
            await coordination.initialize()
            await coordination.register_agent("cli-agent-a", name="CLI Agent A", agent_type="cli")
            task_id = await coordination.create_task(
                title="CLI Inspect Task",
                agent="cli-agent-a",
                description="Inspect tasks command output.",
            )
            await coordination.claim_task(
                task_id=task_id,
                aspect="inspection",
                agent="cli-agent-a",
                ttl_minutes=10,
            )
            completed_task_id = await coordination.create_task(
                title="CLI Completed Task",
                agent="cli-agent-a",
                description="Task completed to validate --all path.",
            )
            await coordination.complete_task(completed_task_id, "cli-agent-a")

        asyncio.run(_seed())

        runner = CliRunner()
        agents = runner.invoke(cli, ["--data-dir", str(temp_dir), "inspect", "agents"])
        assert agents.exit_code == 0, agents.output
        assert "Agents (" in agents.output
        assert "cli-agent-a" in agents.output

        tasks = runner.invoke(cli, ["--data-dir", str(temp_dir), "inspect", "tasks"])
        assert tasks.exit_code == 0, tasks.output
        assert "Tasks (" in tasks.output
        assert "[OPEN] CLI Inspect Task" in tasks.output
        assert "claim:" in tasks.output
        assert "CLI Completed Task" not in tasks.output

        all_tasks = runner.invoke(cli, ["--data-dir", str(temp_dir), "inspect", "tasks", "--all"])
        assert all_tasks.exit_code == 0, all_tasks.output
        assert "[COMPLETED] CLI Completed Task" in all_tasks.output

    def test_inspect_health_exit_code_paths(self, temp_dir, monkeypatch):
        config = LithosConfig(storage=StorageConfig(data_dir=temp_dir))
        config.ensure_directories()
        set_config(config)

        runner = CliRunner()
        healthy = runner.invoke(cli, ["--data-dir", str(temp_dir), "inspect", "health"])
        assert healthy.exit_code == 0, healthy.output
        assert "Backend health" in healthy.output
        assert "tantivy: ok" in healthy.output
        assert "chroma: ok" in healthy.output

        from lithos import search as search_module

        class _BrokenCollection:
            def count(self):
                raise RuntimeError("forced health failure")

        class _BrokenSearchEngine:
            def __init__(self, _config):
                self.tantivy = type("T", (), {"index": object()})()
                self.chroma = type("C", (), {"collection": _BrokenCollection()})()

        monkeypatch.setattr(search_module, "SearchEngine", _BrokenSearchEngine)
        unhealthy = runner.invoke(cli, ["--data-dir", str(temp_dir), "inspect", "health"])
        assert unhealthy.exit_code == 1, unhealthy.output
        assert "Backend health" in unhealthy.output
        assert "chroma: unavailable: forced health failure" in unhealthy.output

    def test_serve_stdio_and_sse_paths(self, temp_dir, monkeypatch):
        config = LithosConfig(storage=StorageConfig(data_dir=temp_dir))
        config.ensure_directories()
        set_config(config)

        calls = {"watch_started": 0, "watch_stopped": 0, "stdio": 0, "sse": 0}

        class _DummyMCP:
            async def run_stdio_async(self, show_banner=False):
                calls["stdio"] += 1

            async def run_http_async(self, **kwargs):
                assert kwargs["transport"] == "sse"
                assert kwargs["host"] == "127.0.0.1"
                assert kwargs["port"] == 8766
                assert kwargs["path"] == "/sse"
                calls["sse"] += 1

        class _DummyServer:
            def __init__(self):
                self.mcp = _DummyMCP()

            async def initialize(self):
                return None

            def start_file_watcher(self):
                calls["watch_started"] += 1

            def stop_file_watcher(self):
                calls["watch_stopped"] += 1

        monkeypatch.setattr("lithos.server.create_server", lambda _cfg: _DummyServer())

        runner = CliRunner()
        stdio = runner.invoke(cli, ["--data-dir", str(temp_dir), "serve", "--no-watch"])
        assert stdio.exit_code == 0, stdio.output
        assert "Starting MCP server (stdio transport)..." in stdio.output

        sse = runner.invoke(
            cli,
            [
                "--data-dir",
                str(temp_dir),
                "serve",
                "--transport",
                "sse",
                "--host",
                "127.0.0.1",
                "--port",
                "8766",
                "--watch",
            ],
        )
        assert sse.exit_code == 0, sse.output
        assert "Listening on http://127.0.0.1:8766" in sse.output

        assert calls["stdio"] == 1
        assert calls["sse"] == 1
        assert calls["watch_started"] == 1
        assert calls["watch_stopped"] == 0

    def test_serve_keyboard_interrupt_stops_watcher(self, temp_dir, monkeypatch):
        config = LithosConfig(storage=StorageConfig(data_dir=temp_dir))
        config.ensure_directories()
        set_config(config)

        calls = {"stop": 0}

        class _DummyServer:
            mcp = SimpleNamespace()

            async def initialize(self):
                return None

            def start_file_watcher(self):
                return None

            def stop_file_watcher(self):
                calls["stop"] += 1

        monkeypatch.setattr("lithos.server.create_server", lambda _cfg: _DummyServer())

        def _raise_keyboard_interrupt(coro):
            coro.close()
            raise KeyboardInterrupt

        monkeypatch.setattr("lithos.cli.asyncio.run", _raise_keyboard_interrupt)

        runner = CliRunner()
        result = runner.invoke(cli, ["--data-dir", str(temp_dir), "serve", "--watch"])
        assert result.exit_code == 0, result.output
        assert "Shutting down..." in result.output
        assert calls["stop"] == 1
