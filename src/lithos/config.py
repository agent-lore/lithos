"""Configuration management for Lithos."""

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Canonical LCMA note_type values — must match VALID_NOTE_TYPES in knowledge.py
_LCMA_NOTE_TYPES = frozenset(
    {"observation", "agent_finding", "summary", "concept", "task_record", "hypothesis"}
)

# Default rerank weights keyed by scout name (minus scout_ prefix)
_DEFAULT_RERANK_WEIGHTS: dict[str, float] = {
    "vector": 0.25,
    "lexical": 0.18,
    "exact_alias": 0.10,
    "tags_recency": 0.07,
    "freshness": 0.04,
    "provenance": 0.04,
    "task_context": 0.04,
    "graph": 0.13,
    "coactivation": 0.10,
    "source_url": 0.05,
}

_DEFAULT_NOTE_TYPE_PRIORS: dict[str, float] = {
    "agent_finding": 0.6,
    "concept": 0.45,
    "hypothesis": 0.5,
    "observation": 0.5,
    "summary": 0.55,
    "task_record": 0.35,
}


class ServerConfig(BaseModel):
    """Server configuration."""

    transport: Literal["stdio", "sse"] = "stdio"
    host: str = "127.0.0.1"
    port: int = 8765
    watch_files: bool = True


class StorageConfig(BaseModel):
    """Storage paths configuration."""

    data_dir: Path = Path("./data")
    knowledge_subdir: str = "knowledge"
    max_content_size_bytes: int = 1_000_000

    @property
    def knowledge_path(self) -> Path:
        """Get absolute path to knowledge directory."""
        return self.data_dir / self.knowledge_subdir

    @property
    def tantivy_path(self) -> Path:
        """Get path to Tantivy index."""
        return self.data_dir / ".tantivy"

    @property
    def chroma_path(self) -> Path:
        """Get path to ChromaDB data."""
        return self.data_dir / ".chroma"

    @property
    def graph_path(self) -> Path:
        """Get path to graph cache."""
        return self.data_dir / ".graph"

    @property
    def lithos_store_path(self) -> Path:
        """Get path to .lithos/ store directory (SQLite DBs, receipts, migrations)."""
        return self.data_dir / ".lithos"

    @property
    def coordination_db_path(self) -> Path:
        """Get path to coordination database."""
        return self.lithos_store_path / "coordination.db"

    @property
    def edges_db_path(self) -> Path:
        """Get path to LCMA edges database."""
        return self.lithos_store_path / "edges.db"

    @property
    def stats_db_path(self) -> Path:
        """Get path to LCMA stats database."""
        return self.lithos_store_path / "stats.db"


class SearchConfig(BaseModel):
    """Search configuration."""

    embedding_model: str = "all-MiniLM-L6-v2"
    semantic_threshold: float = 0.3
    max_results: int = 50
    chunk_size: int = 500
    chunk_max: int = 1000


class CoordinationConfig(BaseModel):
    """Coordination configuration."""

    claim_default_ttl_minutes: int = 60  # minutes
    claim_max_ttl_minutes: int = 480  # minutes


class TelemetryConfig(BaseModel):
    """OpenTelemetry configuration."""

    enabled: bool = False
    endpoint: str | None = None  # OTLP HTTP endpoint, e.g. "http://otel-collector:4318"
    console_fallback: bool = False  # Print spans to stdout when no endpoint
    service_name: str = "lithos"
    export_interval_ms: int = 30_000  # Metrics export interval


class IndexConfig(BaseModel):
    """Index configuration."""

    rebuild_on_start: bool = False
    watch_debounce_ms: int = 500


class EventsConfig(BaseModel):
    """Internal event bus configuration."""

    enabled: bool = True
    event_buffer_size: int = 500
    subscriber_queue_size: int = 100

    # SSE delivery surface
    sse_enabled: bool = True
    max_sse_clients: int = 50


class LcmaConfig(BaseModel):
    """LCMA (Lithos Cognitive Memory Architecture) configuration subtree."""

    enabled: bool = True
    enrich_drain_interval_minutes: int = 5
    rerank_weights: dict[str, float] = Field(default_factory=lambda: dict(_DEFAULT_RERANK_WEIGHTS))
    note_type_priors: dict[str, float] = Field(
        default_factory=lambda: dict(_DEFAULT_NOTE_TYPE_PRIORS)
    )
    temperature_default: float = 0.5
    temperature_edge_threshold: int = 50
    wm_eviction_days: int = 7
    max_enrich_attempts: int = 3
    decay_inactive_days: int = 7
    sweep_interval_hours: int = 24
    sweep_startup_delay_minutes: int = 10
    llm_provider: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _fill_and_renormalize_rerank_weights(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        weights = data.get("rerank_weights")
        if weights is None:
            return data
        if not isinstance(weights, dict):
            return data
        valid_keys = set(_DEFAULT_RERANK_WEIGHTS.keys())
        # Reject unknown keys
        unknown = set(weights.keys()) - valid_keys
        if unknown:
            raise ValueError(
                f"Unknown rerank_weights keys: {sorted(unknown)}. "
                f"Allowed keys: {sorted(valid_keys)}"
            )
        # Fill missing keys with defaults and renormalize
        missing = valid_keys - set(weights.keys())
        if missing:
            for key in missing:
                weights[key] = _DEFAULT_RERANK_WEIGHTS[key]
            total = sum(weights.values())
            if total <= 0:
                raise ValueError(
                    f"rerank_weights sum must be positive after filling missing keys, got {total:.4f}"
                )
            weights = {k: v / total for k, v in weights.items()}
        else:
            # All keys present — assert sum ≈ 1.0
            total = sum(weights.values())
            if abs(total - 1.0) > 0.01:
                raise ValueError(f"rerank_weights must sum to ~1.0, got {total:.4f}")
        data["rerank_weights"] = weights
        return data

    @model_validator(mode="before")
    @classmethod
    def _fill_and_validate_note_type_priors(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        priors = data.get("note_type_priors")
        if priors is None:
            return data
        if not isinstance(priors, dict):
            return data
        # Reject unknown keys
        unknown = set(priors.keys()) - _LCMA_NOTE_TYPES
        if unknown:
            raise ValueError(
                f"Unknown note_type_priors keys: {sorted(unknown)}. "
                f"Allowed keys: {sorted(_LCMA_NOTE_TYPES)}"
            )
        # Fill missing keys with differentiated defaults
        for nt in _LCMA_NOTE_TYPES:
            if nt not in priors:
                priors[nt] = _DEFAULT_NOTE_TYPE_PRIORS[nt]
        data["note_type_priors"] = priors
        return data


class LithosConfig(BaseSettings):
    """Main Lithos configuration."""

    model_config = SettingsConfigDict(
        env_prefix="LITHOS_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    coordination: CoordinationConfig = Field(default_factory=CoordinationConfig)
    index: IndexConfig = Field(default_factory=IndexConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    events: EventsConfig = Field(default_factory=EventsConfig)
    lcma: LcmaConfig = Field(default_factory=LcmaConfig)

    @model_validator(mode="after")
    def _apply_backward_compat_env_overrides(self) -> "LithosConfig":
        """Apply backward-compatible flat env var overrides.

        Handles: LITHOS_DATA_DIR, LITHOS_PORT, LITHOS_HOST,
        LITHOS_OTEL_ENABLED, OTEL_EXPORTER_OTLP_ENDPOINT

        Env vars are only applied when the corresponding field was **not**
        explicitly set via a constructor argument.  This means
        ``LithosConfig(storage=StorageConfig(data_dir=tmp))`` is respected
        even when ``LITHOS_DATA_DIR`` is set in the environment.
        """
        if (data_dir := os.environ.get("LITHOS_DATA_DIR")) and (
            "data_dir" not in self.storage.model_fields_set
        ):
            self.storage.data_dir = Path(data_dir)
        if (port := os.environ.get("LITHOS_PORT")) and ("port" not in self.server.model_fields_set):
            try:
                self.server.port = int(port)
            except ValueError:
                raise ValueError(f"LITHOS_PORT must be a valid integer, got {port!r}") from None
        if (host := os.environ.get("LITHOS_HOST")) and ("host" not in self.server.model_fields_set):
            self.server.host = host
        if (otel_enabled := os.environ.get("LITHOS_OTEL_ENABLED")) and (
            "enabled" not in self.telemetry.model_fields_set
        ):
            self.telemetry.enabled = otel_enabled.lower() in ("1", "true")
        if (otlp_endpoint := os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")) and (
            "endpoint" not in self.telemetry.model_fields_set
        ):
            self.telemetry.endpoint = otlp_endpoint
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> "LithosConfig":
        """Load configuration from YAML file."""
        if not path.exists():
            return cls()

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        return cls(**data)

    def ensure_directories(self) -> None:
        """Create all required directories."""
        self.storage.knowledge_path.mkdir(parents=True, exist_ok=True)
        self.storage.tantivy_path.mkdir(parents=True, exist_ok=True)
        self.storage.chroma_path.mkdir(parents=True, exist_ok=True)
        self.storage.graph_path.mkdir(parents=True, exist_ok=True)
        self.storage.lithos_store_path.mkdir(parents=True, exist_ok=True)


# Global config instance (set during startup)
_config: LithosConfig | None = None


def load_config(path: str | None = None) -> LithosConfig:
    """Load configuration from file and/or environment.

    Args:
        path: Optional path to YAML config file

    Returns:
        Loaded configuration with environment overrides applied
    """
    # Start with defaults or load from file; env var overrides are applied
    # automatically via LithosConfig._apply_backward_compat_env_overrides.
    if path:
        config_path = Path(path)
        return LithosConfig.from_yaml(config_path) if config_path.exists() else LithosConfig()
    return LithosConfig()


def get_config() -> LithosConfig:
    """Get the global configuration instance."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def set_config(config: LithosConfig) -> None:
    """Set the global configuration instance."""
    if config is None:
        raise TypeError("config must be a LithosConfig instance, not None")
    global _config
    _config = config


def _reset_config() -> None:
    """Reset the global config to None. For testing only."""
    global _config
    _config = None
