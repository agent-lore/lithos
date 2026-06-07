"""CLI regression tests."""

import asyncio

from click.testing import CliRunner

from lithos.cli import cli
from lithos.knowledge import KnowledgeManager


def test_inspect_doc_shows_created_and_updated_timestamps(test_config):
    """`lithos inspect doc` should use the metadata timestamp field names that exist."""
    knowledge = KnowledgeManager(test_config)
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


class TestExtractEntitiesCommand:
    """`lithos extract-entities` — one-shot corpus entity repair (#313)."""

    def _setup_docs(self, test_config):
        """Three docs: junk-marked (stale), agent-curated (markerless), empty."""
        knowledge = KnowledgeManager(test_config)

        async def _create():
            stale = (
                await knowledge.create(
                    title="Stale Doc",
                    content="Lithos uses [[NetworkX]] for graph operations.",
                    agent="influx",
                )
            ).document
            await knowledge.update(
                id=stale.id,
                agent="lithos-enrich",
                entities=["Summary", "Highly"],
                entities_extractor=1,
            )
            curated = (
                await knowledge.create(
                    title="Curated Doc",
                    content="Discusses kalman filtering at length.",
                    agent="human",
                )
            ).document
            await knowledge.update(id=curated.id, agent="human", entities=["Kalman Filter"])
            empty = (
                await knowledge.create(
                    title="Empty Doc",
                    content="Mentions [[ChromaDB]] in passing.",
                    agent="influx",
                )
            ).document
            return stale.id, curated.id, empty.id

        return knowledge, asyncio.run(_create())

    def _read_entities(self, test_config, doc_id):
        knowledge = KnowledgeManager(test_config)

        async def _read():
            doc, _ = await knowledge.read(id=doc_id)
            return doc.metadata.entities, doc.metadata.entities_extractor

        return asyncio.run(_read())

    def test_dry_run_writes_nothing(self, test_config):
        _, (stale_id, _curated_id, _empty_id) = self._setup_docs(test_config)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--data-dir", str(test_config.storage.data_dir), "extract-entities", "--dry-run"],
        )
        assert result.exit_code == 0
        entities, marker = self._read_entities(test_config, stale_id)
        assert entities == ["Summary", "Highly"]
        assert marker == 1

    def test_contract_mode_respects_curation(self, test_config):
        _, (stale_id, curated_id, empty_id) = self._setup_docs(test_config)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--data-dir", str(test_config.storage.data_dir), "extract-entities"]
        )
        assert result.exit_code == 0

        # Stale-marker doc re-extracted
        entities, marker = self._read_entities(test_config, stale_id)
        assert "NetworkX" in entities
        assert "Summary" not in entities
        from lithos.lcma.entities import ENTITY_EXTRACTOR_VERSION

        assert marker == ENTITY_EXTRACTOR_VERSION
        # Curated (markerless) doc untouched
        entities, marker = self._read_entities(test_config, curated_id)
        assert entities == ["Kalman Filter"]
        assert marker is None
        # Empty doc extracted
        entities, marker = self._read_entities(test_config, empty_id)
        assert "ChromaDB" in entities
        assert marker == ENTITY_EXTRACTOR_VERSION

    def test_force_replaces_everywhere(self, test_config):
        _, (_stale_id, curated_id, _empty_id) = self._setup_docs(test_config)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--data-dir", str(test_config.storage.data_dir), "extract-entities", "--force"],
        )
        assert result.exit_code == 0

        # Even the markerless (curated) doc is re-extracted under --force
        entities, marker = self._read_entities(test_config, curated_id)
        assert entities != ["Kalman Filter"]
        from lithos.lcma.entities import ENTITY_EXTRACTOR_VERSION

        assert marker == ENTITY_EXTRACTOR_VERSION

    def test_force_leaves_barren_notes_unstamped(self, test_config):
        """A note with no entities and nothing extractable stays pristine
        under --force — same contract as the enrichment worker."""
        knowledge = KnowledgeManager(test_config)

        async def _create():
            doc = (
                await knowledge.create(
                    title="barren note",
                    content="plain lowercase prose with nothing extractable in it.",
                    agent="human",
                )
            ).document
            return doc.id, doc.metadata.version

        barren_id, version_before = asyncio.run(_create())

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--data-dir", str(test_config.storage.data_dir), "extract-entities", "--force"],
        )
        assert result.exit_code == 0

        fresh = KnowledgeManager(test_config)

        async def _read():
            doc, _ = await fresh.read(id=barren_id)
            return doc.metadata

        meta = asyncio.run(_read())
        assert meta.entities == []
        assert meta.entities_extractor is None
        assert meta.version == version_before
