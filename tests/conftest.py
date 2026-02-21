"""Pytest configuration and fixtures."""

import asyncio
import shutil
import tempfile
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
import pytest_asyncio

from exogram.config import ExogramConfig, StorageConfig, set_config
from exogram.coordination import CoordinationService
from exogram.graph import KnowledgeGraph
from exogram.knowledge import KnowledgeManager
from exogram.search import SearchEngine
from exogram.server import ExogramServer


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for test data."""
    tmp = Path(tempfile.mkdtemp())
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def test_config(temp_dir: Path) -> ExogramConfig:
    """Create test configuration with temporary directories."""
    config = ExogramConfig(
        storage=StorageConfig(data_dir=temp_dir),
    )
    config.ensure_directories()
    set_config(config)
    return config


@pytest.fixture
def knowledge_manager(test_config: ExogramConfig) -> KnowledgeManager:
    """Create knowledge manager for testing."""
    return KnowledgeManager()


@pytest.fixture
def search_engine(test_config: ExogramConfig) -> SearchEngine:
    """Create search engine for testing."""
    return SearchEngine(test_config)


@pytest.fixture
def knowledge_graph(test_config: ExogramConfig) -> KnowledgeGraph:
    """Create knowledge graph for testing."""
    return KnowledgeGraph(test_config)


@pytest_asyncio.fixture
async def coordination_service(
    test_config: ExogramConfig,
) -> AsyncGenerator[CoordinationService, None]:
    """Create coordination service for testing."""
    service = CoordinationService(test_config)
    await service.initialize()
    yield service


@pytest_asyncio.fixture
async def server(test_config: ExogramConfig) -> AsyncGenerator[ExogramServer, None]:
    """Create server for integration testing."""
    srv = ExogramServer(test_config)
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
