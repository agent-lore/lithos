"""CLI regression tests."""

import asyncio

import pytest
from click.testing import CliRunner

from lithos.cli import cli
from lithos.knowledge import KnowledgeManager
from lithos.lcma.stats import StatsStore


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


class TestRecalibrateSalienceCommand:
    """`lithos recalibrate-salience` — one-time backfill of collapsed salience (e7d8ef60)."""

    def _seed(self, test_config):
        async def _s():
            store = StatsStore(test_config)
            await store.open()
            try:
                await store.update_salience("cold", -0.45)  # 0.05, below floor
                await store.update_salience("hot", 0.4)  # 0.9, untouched
            finally:
                await store.close()

        asyncio.run(_s())

    def _salience(self, test_config, node_id):
        async def _r():
            store = StatsStore(test_config)
            await store.open()
            try:
                row = await store.get_node_stats(node_id)
                return row["salience"] if row else None
            finally:
                await store.close()

        return asyncio.run(_r())

    def test_dry_run_reports_without_writing(self, test_config):
        self._seed(test_config)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--data-dir",
                str(test_config.storage.data_dir),
                "recalibrate-salience",
                "--floor",
                "0.3",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "would lift 1" in result.output
        assert self._salience(test_config, "cold") == pytest.approx(0.05)

    def test_lifts_collapsed_rows(self, test_config):
        self._seed(test_config)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--data-dir",
                str(test_config.storage.data_dir),
                "recalibrate-salience",
                "--floor",
                "0.3",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Lifted 1 rows" in result.output
        assert self._salience(test_config, "cold") == pytest.approx(0.3)
        assert self._salience(test_config, "hot") == pytest.approx(0.9)


class TestEvalCommands:
    """`lithos eval export` / `log-report` over the replayable call log (eval suite E1)."""

    async def _record(self, test_config) -> None:
        import json

        from lithos.lcma.call_log import BLOB_SUBDIR, CallLogWriter, LlmCall
        from lithos.lcma.edge_inference import (
            NeighbourInfo,
            build_adjudication_prompt,
            request_payload,
        )

        store = StatsStore(test_config)
        await store.open()
        try:
            candidates = [
                NeighbourInfo("c1", "Note c1", "snippet", 0.6, "default", frozenset(), frozenset())
            ]
            messages = build_adjudication_prompt("Focus", "body", candidates, snippet_chars=700)
            payload = request_payload(
                messages,
                focus_id="focus",
                focus_title="Focus",
                focus_content="body",
                candidates=candidates,
                snippet_chars=700,
            )
            writer = CallLogWriter(store, test_config.storage.lithos_store_path / BLOB_SUBDIR)
            await writer.record(
                LlmCall(
                    node_id="focus",
                    doc_version=1,
                    inference_version=1,
                    model="openai/gpt-6-luna",
                    reasoning_effort="none",
                    request=payload,
                    completion=json.dumps({"judgements": [{"candidate": 1, "relation": "none"}]}),
                    outcome="ok",
                    prompt_tokens=100,
                    completion_tokens=10,
                    reasoning_tokens=0,
                    estimated=False,
                    latency_ms=250.0,
                )
            )
        finally:
            await store.close()

    def test_export_writes_jsonl(self, test_config, tmp_path) -> None:
        import json

        asyncio.run(self._record(test_config))
        out = tmp_path / "cases.jsonl"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--data-dir",
                str(test_config.storage.data_dir),
                "eval",
                "export",
                "--since",
                "1h",
                "--out",
                str(out),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "1 cases exported" in result.output
        lines = out.read_text().splitlines()
        assert len(lines) == 1
        case = json.loads(lines[0])
        assert case["focus"]["id"] == "focus"
        assert case["reference"]["model"] == "openai/gpt-6-luna"
        assert case["messages"][0]["role"] == "system"

    def test_export_to_stdout_and_model_filter(self, test_config) -> None:
        asyncio.run(self._record(test_config))
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--data-dir", str(test_config.storage.data_dir), "eval", "export", "--model", "nope"],
        )
        assert result.exit_code == 0, result.output
        assert "0 cases exported" in result.output

    def test_log_report_text_and_json(self, test_config) -> None:
        import json

        asyncio.run(self._record(test_config))
        runner = CliRunner()
        base = ["--data-dir", str(test_config.storage.data_dir), "eval", "log-report"]
        text = runner.invoke(cli, [*base, "--since", "2d"])
        assert text.exit_code == 0, text.output
        assert "openai/gpt-6-luna (reasoning_effort=none)" in text.output
        as_json = runner.invoke(cli, [*base, "--json"])
        assert as_json.exit_code == 0, as_json.output
        [summary] = json.loads(as_json.output)
        assert summary["calls"] == 1

    def test_bad_since_is_a_usage_error(self, test_config) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--data-dir",
                str(test_config.storage.data_dir),
                "eval",
                "log-report",
                "--since",
                "soon",
            ],
        )
        assert result.exit_code != 0
        assert "expected <N>d" in result.output


class TestEvalRunAndCompare:
    """`lithos eval run --recorded` and `lithos eval compare --gate` over the seed fixtures."""

    def _run(self, runner: CliRunner, runs_dir, recorded: str, model: str):
        return runner.invoke(
            cli,
            [
                "eval",
                "run",
                "--model",
                model,
                "--cases",
                "evals/edges/seed/cases.jsonl",
                "--recorded",
                f"evals/edges/seed/recorded/{recorded}",
                "--runs-dir",
                str(runs_dir),
            ],
        )

    def test_recorded_run_writes_document_and_summary(self, tmp_path) -> None:
        runner = CliRunner()
        result = self._run(runner, tmp_path, "seed-incumbent.jsonl", "seed-incumbent")
        assert result.exit_code == 0, result.output
        assert "[recorded completions]" in result.output
        assert "vs labels (11 slots): relation 90.9%" in result.output
        assert "cost per 1k calls: n/a" in result.output
        files = list(tmp_path.glob("*-seed-incumbent.json"))
        assert len(files) == 1

    def test_json_output_and_gate_verdict(self, tmp_path) -> None:
        import json

        runner = CliRunner()
        incumbent = self._run(runner, tmp_path, "seed-incumbent.jsonl", "openai/gpt-5-mini")
        assert incumbent.exit_code == 0, incumbent.output
        candidate = runner.invoke(
            cli,
            [
                "eval",
                "run",
                "--model",
                "openai/gpt-6-luna",
                "--reasoning-effort",
                "none",
                "--cases",
                "evals/edges/seed/cases.jsonl",
                "--recorded",
                "evals/edges/seed/recorded/seed-constant-direction.jsonl",
                "--runs-dir",
                str(tmp_path),
                "--json",
            ],
        )
        assert candidate.exit_code == 0, candidate.output
        document = json.loads(candidate.stdout)  # stderr carries the "run written" line
        assert document["config"]["reasoning_effort"] == "none"
        assert document["metrics"]["labels"]["direction_agreement"] == 0.0
        inc_file = next(tmp_path.glob("*-openai_gpt-5-mini.json"))
        cand_file = next(tmp_path.glob("*-openai_gpt-6-luna-none.json"))

        table = runner.invoke(cli, ["eval", "compare", str(inc_file), str(cand_file)])
        assert table.exit_code == 0, table.output
        assert "| labels.direction_agreement | 0.750 | 0.000 |" in table.output
        assert "Evidence: same cases, labels and coverage" in table.output

        gated = runner.invoke(cli, ["eval", "compare", str(inc_file), str(cand_file), "--gate"])
        assert gated.exit_code == 1
        assert "NOT PROMOTABLE" in gated.output
        assert "FAIL: direction_agreement" in gated.output

    def test_usage_errors(self, tmp_path) -> None:
        runner = CliRunner()
        no_endpoint = runner.invoke(
            cli, ["eval", "run", "--model", "m", "--runs-dir", str(tmp_path)]
        )
        assert no_endpoint.exit_code != 0
        assert "--base-url is required" in no_endpoint.output
        bad_body = runner.invoke(
            cli,
            [
                "eval",
                "run",
                "--model",
                "m",
                "--cases",
                "evals/edges/seed/cases.jsonl",
                "--recorded",
                "evals/edges/seed/recorded/seed-incumbent.jsonl",
                "--extra-body",
                "{nope",
                "--runs-dir",
                str(tmp_path),
            ],
        )
        assert bad_body.exit_code != 0
        assert "--extra-body" in bad_body.output
        missing = tmp_path / "nope.json"
        assert runner.invoke(cli, ["eval", "compare", str(missing), str(missing)]).exit_code != 0
