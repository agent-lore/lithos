.PHONY: install fmt lint typecheck test test-integration test-all docker-build check diagrams metrics-history metrics-diff

install:
	uv sync

fmt:
	uv run ruff format src/ tests/ scripts/

lint:
	uv run ruff check src/ tests/ scripts/
	uv run ruff format --check src/ tests/ scripts/

typecheck:
	uv run pyright src/ scripts/ tests/guardrail/ tests/test_metrics_diff.py

test:
	uv run pytest -m "not integration" tests/ -q

test-integration:
	uv run pytest -m integration tests/ -q

test-all:
	uv run pytest tests/ -q

docker-build:
	docker build -t lithos:dev -f docker/Dockerfile .

# Regenerate the architecture & domain diagrams and metrics under docs/generated/.
# Run this after changing the code/models and commit the result; CI fails if
# the committed artifacts drift from the code (see .github/workflows/ci.yml).
diagrams:
	uv run pytest tests/guardrail/ -q

# Print the architecture-metrics trend mined from the git history of
# docs/generated/metrics.json. FORMAT=csv|mermaid (default csv).
metrics-history:
	uv run python scripts/metrics_history.py --format $(or $(FORMAT),csv)

# Show the metrics delta between BASE (default origin/main) and the working tree.
metrics-diff:
	@tmp=$$(mktemp); \
	git show $(or $(BASE),origin/main):docs/generated/metrics.json > $$tmp 2>/dev/null || echo '{}' > $$tmp; \
	uv run python scripts/metrics_diff.py $$tmp docs/generated/metrics.json; \
	rm -f $$tmp

check: lint typecheck test
