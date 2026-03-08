"""CLI regression tests."""

import asyncio

from click.testing import CliRunner

from lithos.cli import cli
from lithos.knowledge import KnowledgeManager


def test_inspect_doc_shows_created_and_updated_timestamps(test_config):
    """`lithos inspect doc` should use the metadata timestamp field names that exist."""
    knowledge = KnowledgeManager()
    doc = asyncio.run(
        knowledge.create(
            title="CLI Inspect Test",
            content="A document for CLI timestamp regression coverage.",
            agent="cli-test",
        )
    ).document

    runner = CliRunner()
    result = runner.invoke(
        cli, ["--data-dir", str(test_config.storage.data_dir), "inspect", "doc", doc.id]
    )

    assert result.exit_code == 0
    assert "created:" in result.output
    assert "updated:" in result.output
