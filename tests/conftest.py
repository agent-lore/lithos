"""Pytest configuration and fixtures."""

import shutil
import tempfile
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
import pytest_asyncio

from lithos.config import LithosConfig, StorageConfig, _reset_config, set_config
from lithos.coordination import CoordinationService
from lithos.graph import KnowledgeGraph
from lithos.knowledge import KnowledgeManager
from lithos.search import SearchEngine
from lithos.server import LithosServer

# Env vars that the LithosConfig model_validator reads.
_LITHOS_ENV_VARS = (
    "LITHOS_DATA_DIR",
    "LITHOS_PORT",
    "LITHOS_HOST",
    "LITHOS_OTEL_ENABLED",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for test data."""
    tmp = Path(tempfile.mkdtemp())
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def test_config(
    temp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[LithosConfig, None, None]:
    """Create test configuration with temporary directories.

    Clears all LITHOS_* env vars that the model_validator would read, so
    constructor arguments are always respected.
    """
    for var in _LITHOS_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    config = LithosConfig(
        storage=StorageConfig(data_dir=temp_dir),
    )
    config.ensure_directories()
    set_config(config)
    yield config
    _reset_config()  # Use _reset_config() not set_config(None) — the latter now raises TypeError


@pytest.fixture
def knowledge_manager(test_config: LithosConfig) -> KnowledgeManager:
    """Create knowledge manager for testing."""
    return KnowledgeManager(test_config)


@pytest.fixture
def search_engine(test_config: LithosConfig) -> SearchEngine:
    """Create search engine for testing."""
    return SearchEngine(test_config)


@pytest.fixture
def knowledge_graph(test_config: LithosConfig) -> KnowledgeGraph:
    """Create knowledge graph for testing."""
    return KnowledgeGraph(test_config)


@pytest_asyncio.fixture
async def coordination_service(
    test_config: LithosConfig,
) -> AsyncGenerator[CoordinationService, None]:
    """Create coordination service for testing."""
    service = CoordinationService(test_config)
    await service.initialize()
    yield service


@pytest_asyncio.fixture
async def server(test_config: LithosConfig) -> AsyncGenerator[LithosServer, None]:
    """Create server for integration testing."""
    srv = LithosServer(test_config)
    await srv.initialize()
    yield srv
    srv.stop_file_watcher()


# Sample test data
@pytest.fixture
def sample_markdown() -> str:
    """Sample markdown content for testing."""
    return """This is a test document with some content.

It has multiple paragraphs and [[wiki-links]] to other documents.

## Section One

Some text about topic A with a link to [[another-doc|Another Document]].

## Section Two

More content here about topic B.
"""


@pytest.fixture
def sample_documents() -> list[dict]:
    """Sample documents for bulk testing."""
    return [
        {
            "title": "Python Best Practices",
            "content": "Use type hints, write tests, follow PEP 8. See [[testing-guide]] for more.",
            "tags": ["python", "best-practices"],
        },
        {
            "title": "Testing Guide",
            "content": "Write Python unit tests with pytest. Use fixtures for setup. Mock external dependencies.",
            "tags": ["testing", "python"],
        },
        {
            "title": "Docker Deployment",
            "content": "Use multi-stage builds. Keep images small. Use docker-compose for local dev.",
            "tags": ["docker", "deployment"],
        },
        {
            "title": "API Design",
            "content": "REST APIs should be stateless. Use proper HTTP methods. Version your APIs.",
            "tags": ["api", "design"],
        },
        {
            "title": "Database Optimization",
            "content": "Index frequently queried columns. Use connection pooling. Avoid N+1 queries.",
            "tags": ["database", "performance"],
        },
    ]
