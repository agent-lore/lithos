"""Tests for LLM-adjudicated typed-edge inference (WS1 slice 1).

Pure functions (pre-filter, prompt, parse, endpoint resolution) are tested directly.
The EdgeInferenceEngine is tested over a real StatsStore/EdgeStore/CorpusIntake on
temp dirs with an LlmClient backed by httpx.MockTransport — no network, no model.
The cascade-regression tests prove the origin-stamp guard: an inferred-edge write
must never re-enqueue its endpoints through the enrich worker's event handler.
The authority-boundary tests prove inferred writes can never displace asserted
rows or manually resolved conflicts, and that document/policy/purge resets
genuinely re-adjudicate.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from lithos.config import LithosConfig, LlmConfig
from lithos.corpus_index import CachedMeta
from lithos.edge_store import EdgeStore
from lithos.events import EDGE_UPSERTED, NOTE_CREATED, NOTE_UPDATED, EventBus
from lithos.frontmatter_codec import KnowledgeDocument, KnowledgeMetadata
from lithos.intake import CorpusIntake
from lithos.lcma.call_log import BLOB_SUBDIR, CallLogWriter, encode_request
from lithos.lcma.edge_inference import (
    Adjudication,
    EdgeInferenceEngine,
    NeighbourInfo,
    build_adjudication_prompt,
    edge_endpoints,
    parse_adjudications,
    prefilter_candidates,
)
from lithos.lcma.llm import LlmClient
from lithos.lcma.stats import StatsStore
from lithos.search import SemanticResult

# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def _neighbour(node_id: str = "n1", **overrides: Any) -> NeighbourInfo:
    defaults: dict[str, Any] = {
        "node_id": node_id,
        "title": f"Note {node_id}",
        "snippet": "snippet",
        "similarity": 0.6,
        "namespace": "default",
        "entities": frozenset(),
        "tags": frozenset(),
    }
    defaults.update(overrides)
    return NeighbourInfo(**defaults)


def _prefilter(neighbours: list[NeighbourInfo], **overrides: Any) -> list[NeighbourInfo]:
    defaults: dict[str, Any] = {
        "focus_id": "focus",
        "focus_namespace": "default",
        "focus_entities": frozenset(),
        "focus_tags": frozenset(),
        "min_similarity": 0.35,
        "max_similarity": 0.92,
        "k": 5,
    }
    defaults.update(overrides)
    return prefilter_candidates(neighbours, **defaults)


class TestPrefilterCandidates:
    def test_drops_self_and_cross_namespace(self) -> None:
        kept = _prefilter(
            [
                _neighbour("focus"),  # self
                _neighbour("other-ns", namespace="projects"),
                _neighbour("ok"),
            ]
        )
        assert [n.node_id for n in kept] == ["ok"]

    def test_similarity_band_is_inclusive(self) -> None:
        kept = _prefilter(
            [
                _neighbour("too-far", similarity=0.34),
                _neighbour("low-edge", similarity=0.35),
                _neighbour("high-edge", similarity=0.92),
                _neighbour("near-dup", similarity=0.93),
            ]
        )
        assert {n.node_id for n in kept} == {"low-edge", "high-edge"}

    def test_entity_and_tag_overlap_boost_ranking(self) -> None:
        # Same similarity; entity overlap must outrank tag overlap outrank none.
        kept = _prefilter(
            [
                _neighbour("plain", similarity=0.6),
                _neighbour("tagged", similarity=0.6, tags=frozenset({"t"})),
                _neighbour("entity", similarity=0.6, entities=frozenset({"E"})),
            ],
            focus_entities=frozenset({"E"}),
            focus_tags=frozenset({"t"}),
        )
        assert [n.node_id for n in kept] == ["entity", "tagged", "plain"]

    def test_k_caps_survivors(self) -> None:
        many = [_neighbour(f"n{i}", similarity=0.5 + i * 0.01) for i in range(10)]
        kept = _prefilter(many, k=3)
        assert len(kept) == 3
        assert kept[0].similarity > kept[-1].similarity


class TestBuildAdjudicationPrompt:
    def test_numbers_candidates_and_truncates(self) -> None:
        messages = build_adjudication_prompt(
            "Focus title",
            "X" * 2000,
            [_neighbour("a", snippet="Y" * 2000), _neighbour("b")],
            snippet_chars=100,
        )
        assert messages[0].role == "system"
        user = messages[1].content
        assert "Focus title" in user
        assert "X" * 100 in user and "X" * 101 not in user
        assert "[1] Title: Note a" in user
        assert "[2] Title: Note b" in user
        assert "Y" * 100 in user and "Y" * 101 not in user


class TestParseAdjudications:
    def test_valid_envelope(self) -> None:
        text = json.dumps(
            {
                "judgements": [
                    {
                        "candidate": 1,
                        "relation": "supports",
                        "direction": "candidate_to_focus",
                        "confidence": 0.8,
                        "rationale": "because",
                    }
                ]
            }
        )
        parsed = parse_adjudications(text, 1)
        assert parsed == ([Adjudication(1, "supports", "candidate_to_focus", 0.8, "because")], 0)

    def test_fenced_and_bare_list_accepted(self) -> None:
        entry = {
            "candidate": 1,
            "relation": "refines",
            "direction": "focus_to_candidate",
            "confidence": 0.7,
        }
        fenced = f"```json\n{json.dumps({'judgements': [entry]})}\n```"
        assert parse_adjudications(fenced, 1) is not None
        bare = json.dumps([entry])
        parsed = parse_adjudications(bare, 1)
        assert parsed is not None
        adjudications, problems = parsed
        assert adjudications[0].relation == "refines"
        assert problems == 0

    def test_directional_relation_without_direction_is_dropped(self) -> None:
        """Round-1 review: direction is never invented — an inverted supports/
        depends_on edge is worse than no edge."""
        entries = [
            {"candidate": 1, "relation": "refines", "confidence": 0.7},  # no direction
            {"candidate": 2, "relation": "supports", "direction": "sideways", "confidence": 0.8},
        ]
        parsed = parse_adjudications(json.dumps({"judgements": entries}), 2)
        assert parsed == ([], 4)  # 2 dropped entries + 2 uncovered candidates

    def test_symmetric_relation_ignores_direction(self) -> None:
        entries = [{"candidate": 1, "relation": "contradicts", "confidence": 0.9}]
        parsed = parse_adjudications(json.dumps({"judgements": entries}), 1)
        assert parsed is not None
        adjudications, problems = parsed
        assert adjudications[0].relation == "contradicts"
        assert problems == 0

    def test_fractional_and_boolean_indexes_rejected(self) -> None:
        """1.9 must not silently become candidate 1; True must not become 1."""
        entries = [
            {
                "candidate": 1.9,
                "relation": "supports",
                "direction": "focus_to_candidate",
                "confidence": 0.8,
            },
            {
                "candidate": True,
                "relation": "supports",
                "direction": "focus_to_candidate",
                "confidence": 0.8,
            },
        ]
        parsed = parse_adjudications(json.dumps({"judgements": entries}), 2)
        assert parsed == ([], 4)  # 2 bad indexes + 2 uncovered candidates

    def test_duplicate_candidate_keeps_first_and_counts_problem(self) -> None:
        entries = [
            {
                "candidate": 1,
                "relation": "supports",
                "direction": "focus_to_candidate",
                "confidence": 0.8,
            },
            {"candidate": 1, "relation": "contradicts", "confidence": 0.9},  # duplicate
        ]
        parsed = parse_adjudications(json.dumps({"judgements": entries}), 1)
        assert parsed is not None
        adjudications, problems = parsed
        assert len(adjudications) == 1 and adjudications[0].relation == "supports"
        assert problems == 1

    def test_missing_coverage_counts_problems(self) -> None:
        """The prompt demands every candidate exactly once; silence is a
        contract violation, while an explicit "none" covers its candidate."""
        entries = [
            {"candidate": 1, "relation": "none", "confidence": 0.9},
        ]
        parsed = parse_adjudications(json.dumps({"judgements": entries}), 3)
        assert parsed == ([], 2)  # candidates 2 and 3 never covered

    def test_invalid_entries_dropped_individually(self) -> None:
        entries = [
            {"candidate": 1, "relation": "none", "confidence": 0.9},  # deliberate none: covers 1
            {"candidate": 2, "relation": "made_up", "confidence": 0.9},  # unknown vocab
            {"candidate": 99, "relation": "supports", "confidence": 0.9},  # bad index
            {"candidate": 2, "relation": "supports", "confidence": 1.7},  # bad confidence
            {
                "candidate": 3,
                "relation": "depends_on",
                "direction": "focus_to_candidate",
                "confidence": 0.75,
            },  # valid
        ]
        parsed = parse_adjudications(json.dumps({"judgements": entries}), 3)
        assert parsed is not None
        adjudications, problems = parsed
        assert len(adjudications) == 1
        assert adjudications[0].relation == "depends_on"
        assert problems == 4  # made_up + bad index + bad confidence + candidate 2 uncovered

    def test_garbage_returns_none(self) -> None:
        assert parse_adjudications("I think these notes are related!", 2) is None
        assert parse_adjudications('{"judgements": "nope"}', 2) is None


class TestEdgeEndpoints:
    def test_directed_follows_judged_direction(self) -> None:
        fwd = Adjudication(1, "supports", "focus_to_candidate", 0.8, "")
        rev = Adjudication(1, "supports", "candidate_to_focus", 0.8, "")
        assert edge_endpoints("f", "c", fwd) == ("f", "c")
        assert edge_endpoints("f", "c", rev) == ("c", "f")

    def test_symmetric_canonicalizes(self) -> None:
        adj = Adjudication(1, "contradicts", "candidate_to_focus", 0.8, "")
        assert edge_endpoints("b-focus", "a-cand", adj) == ("a-cand", "b-focus")
        assert edge_endpoints("a-focus", "b-cand", adj) == ("a-focus", "b-cand")


# ---------------------------------------------------------------------------
# EdgeInferenceEngine over real stores + MockTransport
# ---------------------------------------------------------------------------

NOW = datetime(2026, 7, 25, tzinfo=UTC)


def _doc(node_id: str, *, version: int = 1) -> KnowledgeDocument:
    metadata = KnowledgeMetadata(
        id=node_id,
        title=f"Note {node_id}",
        author="tester",
        created_at=NOW,
        updated_at=NOW,
        version=version,
    )
    return KnowledgeDocument(
        id=node_id,
        title=metadata.title,
        content="Some content about caching strategies.",
        metadata=metadata,
        path=Path(f"{node_id}.md"),
    )


def _cached_meta(namespace: str = "default") -> CachedMeta:
    return CachedMeta(
        title="t",
        author="tester",
        tags=[],
        updated_at=NOW,
        path=Path("x.md"),
        namespace=namespace,
    )


def _mock_knowledge(doc: KnowledgeDocument) -> MagicMock:
    km = MagicMock()
    km.has_document = MagicMock(return_value=True)
    km.read = AsyncMock(return_value=(doc, False))
    km.get_cached_meta = MagicMock(return_value=_cached_meta())
    return km


def _mock_search(results: list[SemanticResult]) -> MagicMock:
    search = MagicMock()
    search.semantic_search = MagicMock(return_value=results)
    return search


def _semantic_hit(node_id: str, similarity: float = 0.6) -> SemanticResult:
    return SemanticResult(
        id=node_id,
        title=f"Note {node_id}",
        snippet="neighbour snippet",
        similarity=similarity,
        path=f"{node_id}.md",
    )


def _adjudication_body(judgements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "choices": [
            {"message": {"role": "assistant", "content": json.dumps({"judgements": judgements})}}
        ],
        "usage": {"prompt_tokens": 200, "completion_tokens": 50},
    }


class _EngineHarness:
    """Real StatsStore/EdgeStore/intake + mocked knowledge/search + MockTransport LLM."""

    def __init__(
        self,
        test_config: LithosConfig,
        stats_store: StatsStore,
        edge_store: EdgeStore,
        *,
        handler: Any,
        llm_overrides: dict[str, Any] | None = None,
        search_results: list[SemanticResult] | None = None,
        doc: KnowledgeDocument | None = None,
        call_log: bool = False,
    ) -> None:
        self.calls = {"n": 0}

        def counting_handler(request: httpx.Request) -> httpx.Response:
            self.calls["n"] += 1
            return handler(request)

        llm_config = LlmConfig(
            base_url="http://ollama:11434/v1",
            model="test-model",
            **(llm_overrides or {}),
        )
        self.doc = doc or _doc("focus")
        self.knowledge = _mock_knowledge(self.doc)
        self.event_bus = EventBus(test_config.events)
        self.intake = CorpusIntake(
            knowledge=self.knowledge,
            search=MagicMock(),
            graph=MagicMock(),
            coordination=AsyncMock(),
            event_bus=self.event_bus,
            edge_store=edge_store,
        )
        self.edge_store = edge_store
        self.stats_store = stats_store
        self.call_log = (
            CallLogWriter(stats_store, test_config.storage.lithos_store_path / BLOB_SUBDIR)
            if call_log
            else None
        )
        self.engine = EdgeInferenceEngine(
            config=llm_config,
            client=LlmClient(llm_config, transport=httpx.MockTransport(counting_handler)),
            stats_store=stats_store,
            knowledge=self.knowledge,
            search=_mock_search(
                search_results if search_results is not None else [_semantic_hit("cand")]
            ),
            intake=self.intake,
            agent="lithos-enrich",
            call_log=self.call_log,
        )

    async def close(self) -> None:
        await self.engine.close()


@pytest.fixture
async def stats_store(test_config: LithosConfig):
    store = StatsStore(test_config)
    await store.open()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
async def edge_store(test_config: LithosConfig):
    store = EdgeStore(test_config)
    await store.open()
    try:
        yield store
    finally:
        await store.close()


_GOOD_JUDGEMENT = {
    "candidate": 1,
    "relation": "supports",
    "direction": "focus_to_candidate",
    "confidence": 0.8,
    "rationale": "focus provides evidence",
}


class TestEdgeInferenceEngine:
    async def test_happy_path_writes_inferred_edge_and_ledgers(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        harness = _EngineHarness(
            test_config,
            stats_store,
            edge_store,
            handler=lambda req: httpx.Response(200, json=_adjudication_body([_GOOD_JUDGEMENT])),
        )
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
        finally:
            await harness.close()

        edges = await edge_store.list_edges(from_id="focus", to_id="cand")
        assert len(edges) == 1
        edge = edges[0]
        assert edge["type"] == "supports"
        assert edge["weight"] == pytest.approx(0.8)
        assert edge["provenance_type"] == "inferred"
        assert edge["provenance_actor"] == "lithos-enrich"
        evidence = json.loads(str(edge["evidence"]))
        assert evidence["model"] == "test-model"
        assert evidence["rationale"] == "focus provides evidence"

        assert await stats_store.has_edge_inference("focus", 1, 1)
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        assert await stats_store.get_llm_spend(day) == (250, 1)

    async def test_op_log_short_circuits_second_run(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        harness = _EngineHarness(
            test_config,
            stats_store,
            edge_store,
            handler=lambda req: httpx.Response(200, json=_adjudication_body([_GOOD_JUDGEMENT])),
        )
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
            await harness.engine.maybe_infer("focus", [NOTE_UPDATED])
        finally:
            await harness.close()
        assert harness.calls["n"] == 1  # second run skipped via op-log

    async def test_edge_triggers_never_infer(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        harness = _EngineHarness(
            test_config,
            stats_store,
            edge_store,
            handler=lambda req: httpx.Response(200, json=_adjudication_body([_GOOD_JUDGEMENT])),
        )
        try:
            await harness.engine.maybe_infer("focus", [EDGE_UPSERTED])
            await harness.engine.maybe_infer("focus", "note.created")  # non-collection shape
        finally:
            await harness.close()
        assert harness.calls["n"] == 0
        assert not await stats_store.has_edge_inference("focus", 1, 1)

    async def test_budget_exhausted_skips_without_op_log(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        await stats_store.record_llm_spend(day, 999_999)
        harness = _EngineHarness(
            test_config,
            stats_store,
            edge_store,
            handler=lambda req: httpx.Response(200, json=_adjudication_body([_GOOD_JUDGEMENT])),
        )
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
        finally:
            await harness.close()
        assert harness.calls["n"] == 0
        # No op-log: the node retries once budget frees up (next create/update).
        assert not await stats_store.has_edge_inference("focus", 1, 1)

    async def test_call_cap_skips_and_reset_reopens(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        harness = _EngineHarness(
            test_config,
            stats_store,
            edge_store,
            handler=lambda req: httpx.Response(200, json=_adjudication_body([_GOOD_JUDGEMENT])),
            llm_overrides={"max_calls_per_drain": 0},
        )
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
            assert harness.calls["n"] == 0
        finally:
            await harness.close()

    async def test_llm_error_completes_without_op_log(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("endpoint down")

        harness = _EngineHarness(test_config, stats_store, edge_store, handler=handler)
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])  # must not raise
        finally:
            await harness.close()
        assert not await stats_store.has_edge_inference("focus", 1, 1)
        assert await edge_store.list_edges(from_id="focus") == []

    async def test_parse_failure_records_op_log(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        body = {
            "choices": [{"message": {"role": "assistant", "content": "not json at all"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        }
        harness = _EngineHarness(
            test_config, stats_store, edge_store, handler=lambda req: httpx.Response(200, json=body)
        )
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
        finally:
            await harness.close()
        # Tokens were spent -> op-log recorded so the budget can't be re-burned.
        assert await stats_store.has_edge_inference("focus", 1, 1)
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        assert (await stats_store.get_llm_spend(day))[0] == 110
        assert await edge_store.list_edges(from_id="focus") == []

    async def test_no_candidates_records_op_log_without_call(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        harness = _EngineHarness(
            test_config,
            stats_store,
            edge_store,
            handler=lambda req: httpx.Response(200, json=_adjudication_body([])),
            search_results=[],
        )
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
        finally:
            await harness.close()
        assert harness.calls["n"] == 0
        assert await stats_store.has_edge_inference("focus", 1, 1)

    async def test_confidence_floor_filters_judgements(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        weak = dict(_GOOD_JUDGEMENT, confidence=0.4)
        harness = _EngineHarness(
            test_config,
            stats_store,
            edge_store,
            handler=lambda req: httpx.Response(200, json=_adjudication_body([weak])),
        )
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
        finally:
            await harness.close()
        assert await edge_store.list_edges(from_id="focus") == []
        assert await stats_store.has_edge_inference("focus", 1, 1)


class TestAuthorityBoundary:
    """Round-1 review finding 1: inferred writes must never displace asserted
    rows or manually resolved conflicts — provenance is a one-way boundary."""

    async def test_asserted_edge_survives_inference(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        from lithos.intake import EdgeRequest

        harness = _EngineHarness(
            test_config,
            stats_store,
            edge_store,
            handler=lambda req: httpx.Response(200, json=_adjudication_body([_GOOD_JUDGEMENT])),
        )
        await harness.intake.assert_edge(
            "human-reviewer",
            EdgeRequest(
                from_id="focus",
                to_id="cand",
                edge_type="supports",
                weight=1.0,
                namespace="default",
                provenance_actor="human-reviewer",
                provenance_type="asserted",
                evidence="manual",
            ),
        )
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
        finally:
            await harness.close()

        edges = await edge_store.list_edges(from_id="focus", to_id="cand")
        assert len(edges) == 1
        edge = edges[0]
        # Every authoritative column untouched by the blocked inferred write.
        assert edge["provenance_type"] == "asserted"
        assert edge["provenance_actor"] == "human-reviewer"
        assert edge["evidence"] == "manual"
        assert edge["weight"] == pytest.approx(1.0)
        # The run itself completed: op-log recorded with nothing written.
        assert await stats_store.has_edge_inference("focus", 1, 1)

    async def test_resolved_conflict_survives_reinference(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        """A manually resolved inferred contradiction keeps its resolution."""
        contradiction = {
            "candidate": 1,
            "relation": "contradicts",
            "confidence": 0.95,
            "rationale": "still contradicts",
        }
        # Canonical symmetric endpoints for (focus, cand): "cand" <= "focus".
        await edge_store.upsert(
            from_id="cand",
            to_id="focus",
            edge_type="contradicts",
            weight=0.9,
            namespace="default",
            provenance_actor="human-reviewer",
            provenance_type="inferred",
            evidence="original rationale",
            conflict_state="resolved",
        )
        harness = _EngineHarness(
            test_config,
            stats_store,
            edge_store,
            handler=lambda req: httpx.Response(200, json=_adjudication_body([contradiction])),
        )
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
        finally:
            await harness.close()

        edges = await edge_store.list_edges(from_id="cand", to_id="focus")
        assert len(edges) == 1
        edge = edges[0]
        assert edge["conflict_state"] == "resolved"
        assert edge["provenance_actor"] == "human-reviewer"
        assert edge["evidence"] == "original rationale"
        assert edge["weight"] == pytest.approx(0.9)


class TestReinference:
    """Round-1 review finding 2: document/policy/purge resets must genuinely
    re-adjudicate — existing inferred pairs are refreshed, not excluded."""

    @staticmethod
    def _mutable_handler(judgement: dict[str, Any]):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_adjudication_body([dict(judgement)]))

        return handler

    async def test_document_version_bump_readjudicates_and_refreshes(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        judgement = dict(_GOOD_JUDGEMENT)
        harness = _EngineHarness(
            test_config, stats_store, edge_store, handler=self._mutable_handler(judgement)
        )
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
            assert harness.calls["n"] == 1

            # The note changes: version 2, and the model now judges differently.
            harness.knowledge.read = AsyncMock(return_value=(_doc("focus", version=2), False))
            judgement["confidence"] = 0.9
            judgement["rationale"] = "updated evidence"
            await harness.engine.maybe_infer("focus", [NOTE_UPDATED])
            assert harness.calls["n"] == 2  # genuinely re-adjudicated
        finally:
            await harness.close()

        edges = await edge_store.list_edges(from_id="focus", to_id="cand")
        assert len(edges) == 1  # refreshed in place, not duplicated
        assert edges[0]["weight"] == pytest.approx(0.9)
        assert json.loads(str(edges[0]["evidence"]))["rationale"] == "updated evidence"
        assert await stats_store.has_edge_inference("focus", 1, 1)
        assert await stats_store.has_edge_inference("focus", 2, 1)

    async def test_ledger_purge_enables_actual_readjudication(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        harness = _EngineHarness(
            test_config,
            stats_store,
            edge_store,
            handler=lambda req: httpx.Response(200, json=_adjudication_body([_GOOD_JUDGEMENT])),
        )
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
            assert harness.calls["n"] == 1
            purged = await stats_store.purge_edge_inference_log()
            assert purged == 1
            await harness.engine.maybe_infer("focus", [NOTE_UPDATED])
            assert harness.calls["n"] == 2  # operator reset re-adjudicates
        finally:
            await harness.close()
        edges = await edge_store.list_edges(from_id="focus", to_id="cand")
        assert len(edges) == 1  # same pair refreshed via the inferred upsert

    async def test_inference_version_bump_readjudicates(
        self,
        test_config: LithosConfig,
        stats_store: StatsStore,
        edge_store: EdgeStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        harness = _EngineHarness(
            test_config,
            stats_store,
            edge_store,
            handler=lambda req: httpx.Response(200, json=_adjudication_body([_GOOD_JUDGEMENT])),
        )
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
            assert harness.calls["n"] == 1
            monkeypatch.setattr("lithos.lcma.edge_inference.INFERENCE_VERSION", 2)
            await harness.engine.maybe_infer("focus", [NOTE_UPDATED])
            assert harness.calls["n"] == 2  # policy bump re-adjudicates
        finally:
            await harness.close()
        assert await stats_store.has_edge_inference("focus", 1, 1)
        assert await stats_store.has_edge_inference("focus", 1, 2)

    async def test_changed_relation_accumulates_new_edge(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        """A re-adjudication that changes relation type keys a NEW edge; the
        old one remains (additive semantics — reconciliation of superseded
        inferred edges is slice-2 sweep scope, per the PR1 review record)."""
        judgement = dict(_GOOD_JUDGEMENT)
        harness = _EngineHarness(
            test_config, stats_store, edge_store, handler=self._mutable_handler(judgement)
        )
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
            harness.knowledge.read = AsyncMock(return_value=(_doc("focus", version=2), False))
            judgement["relation"] = "refines"
            await harness.engine.maybe_infer("focus", [NOTE_UPDATED])
        finally:
            await harness.close()
        edges = await edge_store.list_edges(from_id="focus", to_id="cand")
        assert {e["type"] for e in edges} == {"supports", "refines"}

    async def test_stale_version_discards_results(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        """Round-1 review finding 4: a note updated mid-LLM-call must not get
        stale edges or a stale completion marker; spend is still recorded."""
        harness = _EngineHarness(
            test_config,
            stats_store,
            edge_store,
            handler=lambda req: httpx.Response(200, json=_adjudication_body([_GOOD_JUDGEMENT])),
        )
        # First read (prompt build) sees v1; the post-call staleness re-read
        # sees v2 — the note changed while the LLM call was in flight.
        harness.knowledge.read = AsyncMock(
            side_effect=[(_doc("focus", version=1), False), (_doc("focus", version=2), False)]
        )
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
        finally:
            await harness.close()

        assert harness.calls["n"] == 1  # tokens were spent...
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        assert (await stats_store.get_llm_spend(day))[0] == 250  # ...and recorded
        assert await edge_store.list_edges(from_id="focus") == []  # no stale edges
        # No completion marker for either version: the queued v2 drain re-runs.
        assert not await stats_store.has_edge_inference("focus", 1, 1)
        assert not await stats_store.has_edge_inference("focus", 2, 1)


class TestCallLogIntegration:
    """Every call exit lands one replayable row (eval suite E1) with the right outcome."""

    async def _only_row(self, stats_store: StatsStore) -> dict[str, Any]:
        rows = await stats_store.list_llm_calls()
        assert len(rows) == 1
        return rows[0]

    async def test_ok_call_logs_verbatim_prompt_and_completion(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        body = _adjudication_body([_GOOD_JUDGEMENT])
        harness = _EngineHarness(
            test_config,
            stats_store,
            edge_store,
            handler=lambda req: httpx.Response(200, json=body),
            llm_overrides={"reasoning_effort": "none"},
            call_log=True,
        )
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
        finally:
            await harness.close()
        row = await self._only_row(stats_store)
        assert (row["outcome"], row["edges_written"]) == ("ok", 1)
        assert (row["node_id"], row["doc_version"], row["inference_version"]) == ("focus", 1, 1)
        assert (row["model"], row["reasoning_effort"]) == ("test-model", "none")
        assert (row["prompt_tokens"], row["completion_tokens"]) == (200, 50)
        assert row["latency_ms"] >= 0
        assert json.loads(str(row["candidates_json"])) == [{"node_id": "cand", "similarity": 0.6}]
        assert harness.call_log is not None
        request = json.loads(harness.call_log.blobs.get(str(row["request_sha"])) or b"")
        assert request["prompt_version"] == 1
        assert request["focus"] == {
            "id": "focus",
            "title": "Note focus",
            "content": "Some content about caching strategies.",
        }
        assert request["candidates"][0]["node_id"] == "cand"
        assert request["messages"][0]["role"] == "system"
        assert "CANDIDATES" in request["messages"][1]["content"]
        assert encode_request(request) == harness.call_log.blobs.get(str(row["request_sha"]))
        completion = harness.call_log.blobs.get(str(row["completion_sha"]))
        assert completion == body["choices"][0]["message"]["content"].encode()

    async def test_partial_outcome_logged(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        harness = _EngineHarness(
            test_config,
            stats_store,
            edge_store,
            handler=lambda req: httpx.Response(200, json=_adjudication_body([])),  # none covered
            call_log=True,
        )
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
        finally:
            await harness.close()
        row = await self._only_row(stats_store)
        assert (row["outcome"], row["edges_written"]) == ("partial", 0)

    async def test_parse_error_logged_with_completion(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        body = {
            "choices": [{"message": {"role": "assistant", "content": "not json at all"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        }
        harness = _EngineHarness(
            test_config,
            stats_store,
            edge_store,
            handler=lambda req: httpx.Response(200, json=body),
            call_log=True,
        )
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
        finally:
            await harness.close()
        row = await self._only_row(stats_store)
        assert row["outcome"] == "parse_error"
        assert harness.call_log is not None
        assert harness.call_log.blobs.get(str(row["completion_sha"])) == b"not json at all"

    async def test_error_logged_without_completion(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("endpoint down")

        harness = _EngineHarness(
            test_config, stats_store, edge_store, handler=handler, call_log=True
        )
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
        finally:
            await harness.close()
        row = await self._only_row(stats_store)
        assert row["outcome"] == "error"
        assert row["completion_sha"] is None
        assert "ConnectError" in str(row["error"])
        assert (row["prompt_tokens"], row["completion_tokens"]) == (0, 0)

    async def test_stale_logged(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        harness = _EngineHarness(
            test_config,
            stats_store,
            edge_store,
            handler=lambda req: httpx.Response(200, json=_adjudication_body([_GOOD_JUDGEMENT])),
            call_log=True,
        )
        harness.knowledge.read = AsyncMock(
            side_effect=[(_doc("focus", version=1), False), (_doc("focus", version=2), False)]
        )
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
        finally:
            await harness.close()
        row = await self._only_row(stats_store)
        assert (row["outcome"], row["edges_written"], row["doc_version"]) == ("stale", 0, 1)

    async def test_log_failure_never_breaks_inference(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        harness = _EngineHarness(
            test_config,
            stats_store,
            edge_store,
            handler=lambda req: httpx.Response(200, json=_adjudication_body([_GOOD_JUDGEMENT])),
            call_log=True,
        )
        stats_store.record_llm_call = AsyncMock(side_effect=RuntimeError("disk full"))  # type: ignore[method-assign]
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
        finally:
            await harness.close()
        assert len(await edge_store.list_edges(from_id="focus", to_id="cand")) == 1

    async def test_no_log_without_writer(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        harness = _EngineHarness(
            test_config,
            stats_store,
            edge_store,
            handler=lambda req: httpx.Response(200, json=_adjudication_body([_GOOD_JUDGEMENT])),
        )
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
        finally:
            await harness.close()
        assert await stats_store.list_llm_calls() == []


class TestWorkerLifecycle:
    """Production construction: engine exists iff llm enabled; stop() closes it."""

    async def test_enabled_worker_builds_engine_and_stop_closes_it(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        from lithos.lcma.enrich import EnrichWorker

        test_config.lcma.llm = LlmConfig(base_url="http://ollama:11434/v1", model="m")
        worker = EnrichWorker(
            config=test_config.lcma,
            event_bus=EventBus(test_config.events),
            stats_store=stats_store,
            projection=MagicMock(edge_store=edge_store),
            knowledge=MagicMock(),
            coordination=AsyncMock(),
            intake=MagicMock(),
            search=MagicMock(),
        )
        assert worker._edge_inference is not None
        assert worker._edge_inference.call_log is not None  # default 30-day retention → on
        await worker.stop()  # must close the engine's httpx client without error

    async def test_zero_retention_disables_call_log_only(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        from lithos.lcma.enrich import EnrichWorker

        test_config.lcma.llm = LlmConfig(
            base_url="http://ollama:11434/v1", model="m", call_log_retention_days=0
        )
        worker = EnrichWorker(
            config=test_config.lcma,
            event_bus=EventBus(test_config.events),
            stats_store=stats_store,
            projection=MagicMock(edge_store=edge_store),
            knowledge=MagicMock(),
            coordination=AsyncMock(),
            intake=MagicMock(),
            search=MagicMock(),
        )
        assert worker._edge_inference is not None
        assert worker._edge_inference.call_log is None
        await worker.stop()

    async def test_disabled_worker_has_no_engine(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        from lithos.lcma.enrich import EnrichWorker

        test_config.lcma.llm = LlmConfig()
        worker = EnrichWorker(
            config=test_config.lcma,
            event_bus=EventBus(test_config.events),
            stats_store=stats_store,
            projection=MagicMock(edge_store=edge_store),
            knowledge=MagicMock(),
            coordination=AsyncMock(),
            intake=MagicMock(),
            search=MagicMock(),
        )
        assert worker._edge_inference is None
        await worker.stop()


class TestCascadeGuard:
    """The origin stamp must stop inferred-edge events from re-enqueuing endpoints."""

    async def test_inferred_edge_event_is_origin_stamped_and_dropped(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        from lithos.lcma.enrich import EnrichWorker

        harness = _EngineHarness(
            test_config,
            stats_store,
            edge_store,
            handler=lambda req: httpx.Response(200, json=_adjudication_body([_GOOD_JUDGEMENT])),
        )
        worker = EnrichWorker(
            config=test_config.lcma,
            event_bus=harness.event_bus,
            stats_store=stats_store,
            projection=MagicMock(edge_store=edge_store),
            knowledge=harness.knowledge,
            coordination=AsyncMock(),
            intake=harness.intake,
            search=MagicMock(),
        )
        queue = harness.event_bus.subscribe(event_types=[EDGE_UPSERTED])
        try:
            await harness.engine.maybe_infer("focus", [NOTE_CREATED])
            event = queue.get_nowait()
            assert event.origin == "enrich"

            before = await stats_store.drain_pending_nodes(max_attempts=3)
            assert before == []  # clean slate
            await worker._handle_event(event)
            after = await stats_store.drain_pending_nodes(max_attempts=3)
            assert after == []  # origin-stamped event re-enqueued nothing
        finally:
            harness.event_bus.unsubscribe(queue)
            await harness.close()

    async def test_unstamped_edge_event_would_reenqueue(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        """Counterfactual proving the guard is load-bearing: the same event without
        the origin stamp re-enqueues both endpoints."""
        from lithos.events import make_edge_upserted_event
        from lithos.lcma.enrich import EnrichWorker

        km = MagicMock()
        km.has_document = MagicMock(return_value=True)
        worker = EnrichWorker(
            config=test_config.lcma,
            event_bus=EventBus(test_config.events),
            stats_store=stats_store,
            projection=MagicMock(edge_store=edge_store),
            knowledge=km,
            coordination=AsyncMock(),
            intake=MagicMock(),
            search=MagicMock(),
        )
        event = make_edge_upserted_event(
            agent="someone",
            edge_id="e1",
            from_id="focus",
            to_id="cand",
            edge_type="supports",
            namespace="default",
        )
        await worker._handle_event(event)
        entries = await stats_store.drain_pending_nodes(max_attempts=3)
        assert {e["node_id"] for e in entries} == {"focus", "cand"}


class TestDrainPathWiring:
    """End-to-end through the worker's drain loop: enqueue -> drain -> inferred edge."""

    async def test_drain_runs_inference_and_resets_call_counter(
        self, test_config: LithosConfig, stats_store: StatsStore, edge_store: EdgeStore
    ) -> None:
        from lithos.lcma.enrich import EnrichWorker

        harness = _EngineHarness(
            test_config,
            stats_store,
            edge_store,
            handler=lambda req: httpx.Response(200, json=_adjudication_body([_GOOD_JUDGEMENT])),
        )
        harness.knowledge.get_doc_sources = MagicMock(return_value=[])
        worker = EnrichWorker(
            config=test_config.lcma,
            event_bus=harness.event_bus,
            stats_store=stats_store,
            projection=MagicMock(edge_store=edge_store, reconcile_node=AsyncMock()),
            knowledge=harness.knowledge,
            coordination=AsyncMock(),
            intake=harness.intake,
            search=MagicMock(),
        )
        # Inject the MockTransport-backed engine (production wiring builds its own
        # client; tests need the seam) and pre-spend the drain counter to prove
        # drain() resets it before processing.
        worker._edge_inference = harness.engine
        harness.engine._calls_this_drain = 999
        worker._extract_entities = AsyncMock()  # keep spaCy out of this test

        await stats_store.enqueue(NOTE_CREATED, node_id="focus")
        try:
            await worker.drain()
        finally:
            await harness.close()

        edges = await edge_store.list_edges(from_id="focus", to_id="cand")
        assert len(edges) == 1 and edges[0]["provenance_type"] == "inferred"
        assert await stats_store.has_edge_inference("focus", 1, 1)
