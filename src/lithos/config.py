"""Configuration management for Lithos."""

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerConfig(BaseModel):
    """Server configuration."""

    transport: Literal["stdio", "sse"] = "stdio"
    host: str = "0.0.0.0"
    port: int = 8765
    watch_files: bool = True


class StorageConfig(BaseModel):
    """Storage paths configuration."""

    data_dir: Path = Path("./data")
    knowledge_subdir: str = "knowledge"

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


class IndexConfig(BaseModel):
    """Index configuration."""

    rebuild_on_start: bool = False
    watch_debounce_ms: int = 500


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
    # Start with defaults or load from file
    if path:
        config_path = Path(path)
        config = LithosConfig.from_yaml(config_path) if config_path.exists() else LithosConfig()
    else:
        config = LithosConfig()

    # Apply environment variable overrides
    env_data_dir = os.environ.get("LITHOS_DATA_DIR")
    if env_data_dir:
        config.storage.data_dir = Path(env_data_dir)

    env_port = os.environ.get("LITHOS_PORT")
    if env_port:
        config.server.port = int(env_port)

    env_host = os.environ.get("LITHOS_HOST")
    if env_host:
        config.server.host = env_host

    return config


def get_config() -> LithosConfig:
    """Get the global configuration instance."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def set_config(config: LithosConfig | None) -> None:
    """Set the global configuration instance."""
    global _config
    _config = config
