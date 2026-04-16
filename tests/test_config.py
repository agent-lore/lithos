"""Tests for config module - configuration management."""

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from lithos.config import (
    CoordinationConfig,
    LithosConfig,
    SearchConfig,
    ServerConfig,
    StorageConfig,
    _reset_config,
    get_config,
    load_config,
    set_config,
)


class TestConfigDefaults:
    """Tests for default configuration values."""

    def test_storage_defaults(self):
        """Storage config has sensible defaults."""
        config = StorageConfig()

        assert config.data_dir == Path("./data")
        assert config.knowledge_subdir == "knowledge"
        assert config.max_content_size_bytes == 1_000_000

    def test_search_defaults(self):
        """Search config has sensible defaults."""
        config = SearchConfig()

        assert config.chunk_size > 0
        assert config.chunk_max >= config.chunk_size
        assert config.semantic_threshold > 0
        assert config.semantic_threshold < 1

    def test_coordination_defaults(self):
        """Coordination config has sensible defaults."""
        config = CoordinationConfig()

        assert config.claim_default_ttl_minutes > 0
        assert config.claim_max_ttl_minutes >= config.claim_default_ttl_minutes

    def test_server_defaults(self):
        """Server config has sensible defaults."""
        config = ServerConfig()

        assert config.host == "127.0.0.1"
        assert config.port > 0
        assert config.watch_files is True

    def test_full_config_defaults(self):
        """Full config assembles all defaults."""
        config = LithosConfig()

        assert config.storage is not None
        assert config.search is not None
        assert config.coordination is not None
        assert config.server is not None


class TestConfigPaths:
    """Tests for path resolution."""

    def test_knowledge_path_computed(self):
        """Knowledge path is computed from data_dir and subdir."""
        config = LithosConfig(
            storage=StorageConfig(
                data_dir=Path("/custom/data"),
                knowledge_subdir="docs",
            )
        )

        assert config.storage.knowledge_path == Path("/custom/data/docs")

    def test_ensure_directories_creates_paths(self):
        """ensure_directories creates required directories."""
        with tempfile.TemporaryDirectory() as tmp:
            config = LithosConfig(storage=StorageConfig(data_dir=Path(tmp)))

            config.ensure_directories()

            assert config.storage.knowledge_path.exists()
            assert config.storage.knowledge_path.is_dir()


class TestConfigLoading:
    """Tests for loading config from files."""

    def test_load_from_yaml_file(self):
        """Load configuration from YAML file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(
                {
                    "storage": {
                        "data_dir": "/custom/path",
                    },
                    "server": {
                        "port": 9999,
                    },
                },
                f,
            )
            f.flush()

            config = load_config(f.name)

            assert config.storage.data_dir == Path("/custom/path")
            assert config.server.port == 9999

            os.unlink(f.name)

    def test_load_missing_file_uses_defaults(self):
        """Missing config file uses defaults."""
        config = load_config("/nonexistent/config.yaml")

        # Should return default config
        assert config is not None
        assert config.server.port == ServerConfig().port

    def test_partial_config_merges_with_defaults(self):
        """Partial config merges with defaults."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(
                {
                    "server": {
                        "port": 8888,
                    },
                    # storage, search, coordination not specified
                },
                f,
            )
            f.flush()

            config = load_config(f.name)

            # Specified value
            assert config.server.port == 8888
            # Default values
            assert config.storage.data_dir == StorageConfig().data_dir
            assert config.search.chunk_size == SearchConfig().chunk_size

            os.unlink(f.name)


class TestConfigEnvironment:
    """Tests for environment variable overrides."""

    def test_env_override_data_dir(self, monkeypatch):
        """LITHOS_DATA_DIR overrides config."""
        monkeypatch.setenv("LITHOS_DATA_DIR", "/env/data")

        config = load_config()

        assert config.storage.data_dir == Path("/env/data")

    def test_env_override_port(self, monkeypatch):
        """LITHOS_PORT overrides config."""
        monkeypatch.setenv("LITHOS_PORT", "7777")

        config = load_config()

        assert config.server.port == 7777

    def test_env_override_host(self, monkeypatch):
        """LITHOS_HOST overrides config."""
        monkeypatch.setenv("LITHOS_HOST", "127.0.0.1")

        config = load_config()

        assert config.server.host == "127.0.0.1"

    def test_env_otel_enabled_numeric(self, monkeypatch):
        """LITHOS_OTEL_ENABLED=1 maps to telemetry.enabled=True."""
        monkeypatch.setenv("LITHOS_OTEL_ENABLED", "1")

        config = load_config()

        assert config.telemetry.enabled is True

    def test_env_otel_enabled_string(self, monkeypatch):
        """LITHOS_OTEL_ENABLED=true maps to telemetry.enabled=True."""
        monkeypatch.setenv("LITHOS_OTEL_ENABLED", "true")

        config = load_config()

        assert config.telemetry.enabled is True

    def test_env_otel_enabled_false(self, monkeypatch):
        """LITHOS_OTEL_ENABLED=false keeps telemetry.enabled=False."""
        monkeypatch.setenv("LITHOS_OTEL_ENABLED", "false")

        config = load_config()

        assert config.telemetry.enabled is False

    def test_env_otlp_endpoint(self, monkeypatch):
        """OTEL_EXPORTER_OTLP_ENDPOINT maps to telemetry.endpoint."""
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")

        config = load_config()

        assert config.telemetry.endpoint == "http://otel-collector:4318"

    def test_env_override_environment(self, monkeypatch):
        """LITHOS_ENVIRONMENT maps to telemetry.environment."""
        monkeypatch.setenv("LITHOS_ENVIRONMENT", "staging")

        config = load_config()

        assert config.telemetry.environment == "staging"

    def test_env_nested_environment(self, monkeypatch):
        """LITHOS_TELEMETRY__ENVIRONMENT also works via the nested delimiter."""
        monkeypatch.setenv("LITHOS_TELEMETRY__ENVIRONMENT", "production")

        config = load_config()

        assert config.telemetry.environment == "production"

    def test_environment_defaults_to_none(self, monkeypatch):
        """telemetry.environment defaults to None when no env var is set."""
        monkeypatch.delenv("LITHOS_ENVIRONMENT", raising=False)
        monkeypatch.delenv("LITHOS_TELEMETRY__ENVIRONMENT", raising=False)

        config = load_config()

        assert config.telemetry.environment is None

    def test_env_port_invalid_raises(self, monkeypatch):
        """LITHOS_PORT with a non-integer value raises a clear error."""
        monkeypatch.setenv("LITHOS_PORT", "abc")

        with pytest.raises(ValueError, match="LITHOS_PORT must be a valid integer"):
            load_config()

    def test_explicit_constructor_arg_not_overridden_by_env(self, monkeypatch):
        """Constructor args take precedence over env vars."""
        monkeypatch.setenv("LITHOS_DATA_DIR", "/env/data")
        monkeypatch.setenv("LITHOS_PORT", "9999")
        monkeypatch.setenv("LITHOS_HOST", "1.2.3.4")

        config = LithosConfig(
            storage=StorageConfig(data_dir=Path("/explicit/path")),
            server=ServerConfig(port=1111, host="10.0.0.1"),
        )

        assert config.storage.data_dir == Path("/explicit/path")
        assert config.server.port == 1111
        assert config.server.host == "10.0.0.1"


class TestConfigSingleton:
    """Tests for global config singleton."""

    def test_set_and_get_config(self):
        """set_config and get_config work together."""
        custom_config = LithosConfig(server=ServerConfig(port=5555))

        set_config(custom_config)
        retrieved = get_config()

        assert retrieved.server.port == 5555

    def test_get_config_returns_default_if_not_set(self):
        """get_config returns default if not explicitly set."""
        # Reset global config
        _reset_config()

        config = get_config()

        assert config is not None
        assert isinstance(config, LithosConfig)
