.PHONY: install fmt lint typecheck test test-integration test-all docker-build check

install:
	uv sync

fmt:
	uv run ruff format src/ tests/

lint:
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/

typecheck:
	uv run pyright src/

test:
	uv run pytest -m "not integration" tests/ -q

test-integration:
	uv run pytest -m integration tests/ -q

test-all:
	uv run pytest tests/ -q

docker-build:
	docker build -t lithos:dev -f docker/Dockerfile .

check: lint typecheck test
