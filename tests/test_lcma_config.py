"""Tests for US-005: LcmaConfig configuration subtree."""

import pytest

from lithos.config import (
    _DEFAULT_NOTE_TYPE_PRIORS,
    _DEFAULT_RERANK_WEIGHTS,
    _LCMA_NOTE_TYPES,
    LcmaConfig,
    LithosConfig,
)


class TestLcmaConfigDefaults:
    """Default construction produces MVP 1 defaults."""

    def test_default_enabled(self) -> None:
        cfg = LcmaConfig()
        assert cfg.enabled is True

    def test_default_enrich_drain_interval(self) -> None:
        cfg = LcmaConfig()
        assert cfg.enrich_drain_interval_minutes == 5

    def test_default_rerank_weights(self) -> None:
        cfg = LcmaConfig()
        assert cfg.rerank_weights == _DEFAULT_RERANK_WEIGHTS

    def test_rerank_weights_sum_to_one(self) -> None:
        cfg = LcmaConfig()
        assert abs(sum(cfg.rerank_weights.values()) - 1.0) < 1e-9

    def test_default_note_type_priors(self) -> None:
        cfg = LcmaConfig()
        assert cfg.note_type_priors == _DEFAULT_NOTE_TYPE_PRIORS
        assert set(cfg.note_type_priors.keys()) == _LCMA_NOTE_TYPES

    def test_default_temperature(self) -> None:
        cfg = LcmaConfig()
        assert cfg.temperature_default == 0.5

    def test_default_temperature_edge_threshold(self) -> None:
        cfg = LcmaConfig()
        assert cfg.temperature_edge_threshold == 50

    def test_default_wm_eviction_days(self) -> None:
        cfg = LcmaConfig()
        assert cfg.wm_eviction_days == 7

    def test_default_llm_provider(self) -> None:
        cfg = LcmaConfig()
        assert cfg.llm_provider is None


class TestLcmaConfigNoteTypePriors:
    """note_type_priors key filling and rejection."""

    def test_missing_keys_filled_with_default(self) -> None:
        cfg = LcmaConfig(note_type_priors={"observation": 0.9})
        assert cfg.note_type_priors["observation"] == 0.9
        # All other keys filled with 0.5
        for nt in _LCMA_NOTE_TYPES - {"observation"}:
            assert cfg.note_type_priors[nt] == 0.5
        assert len(cfg.note_type_priors) == 6

    def test_unknown_keys_rejected(self) -> None:
        with pytest.raises(Exception, match="Unknown note_type_priors keys"):
            LcmaConfig(note_type_priors={"observation": 0.5, "bogus_type": 0.3})

    def test_empty_dict_fills_all_defaults(self) -> None:
        cfg = LcmaConfig(note_type_priors={})
        assert cfg.note_type_priors == _DEFAULT_NOTE_TYPE_PRIORS

    def test_all_keys_provided(self) -> None:
        custom = {nt: 0.8 for nt in _LCMA_NOTE_TYPES}
        cfg = LcmaConfig(note_type_priors=custom)
        assert all(v == 0.8 for v in cfg.note_type_priors.values())


class TestLithosConfigLcmaSubtree:
    """LcmaConfig as a subtree of LithosConfig."""

    def test_default_construction_includes_lcma(self) -> None:
        cfg = LithosConfig()
        assert isinstance(cfg.lcma, LcmaConfig)
        assert cfg.lcma.enabled is True

    def test_enabled_false_loadable_and_queryable(self) -> None:
        cfg = LithosConfig(lcma=LcmaConfig(enabled=False))
        assert cfg.lcma.enabled is False
        # Other defaults still present
        assert cfg.lcma.temperature_default == 0.5

    def test_no_side_effects_on_unrelated_subtrees(self) -> None:
        """Adding lcma config does not change existing field defaults."""
        cfg = LithosConfig()
        assert cfg.server.transport == "stdio"
        assert cfg.server.port == 8765
        assert cfg.storage.data_dir.name == "data"
        assert cfg.search.embedding_model == "all-MiniLM-L6-v2"
        assert cfg.coordination.claim_default_ttl_minutes == 60
        assert cfg.index.rebuild_on_start is False
        assert cfg.telemetry.enabled is False
        assert cfg.events.enabled is True

    def test_env_var_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """LITHOS_LCMA__* env vars override lcma subtree fields."""
        monkeypatch.setenv("LITHOS_LCMA__ENABLED", "false")
        monkeypatch.setenv("LITHOS_LCMA__TEMPERATURE_DEFAULT", "0.7")
        monkeypatch.setenv("LITHOS_LCMA__WM_EVICTION_DAYS", "14")
        cfg = LithosConfig()
        assert cfg.lcma.enabled is False
        assert cfg.lcma.temperature_default == 0.7
        assert cfg.lcma.wm_eviction_days == 14

    def test_env_var_does_not_affect_other_subtrees(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LITHOS_LCMA__ENABLED", "false")
        cfg = LithosConfig()
        # server, storage, etc. unchanged
        assert cfg.server.transport == "stdio"
        assert cfg.storage.data_dir.name == "data"

    def test_defaults_allow_mvp1_no_user_config(self) -> None:
        """MVP 1 functions without any user configuration."""
        cfg = LithosConfig()
        assert cfg.lcma.enabled is True
        assert len(cfg.lcma.rerank_weights) == 7
        assert len(cfg.lcma.note_type_priors) == 6
