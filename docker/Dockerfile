# syntax=docker/dockerfile:1.7
# Lithos - Local shared knowledge base for AI agents
# Multi-stage build for smaller image

# Stage 1: Build dependencies
FROM python:3.11-slim AS builder

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Create virtual environment and install dependencies
RUN uv venv /app/.venv
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"
ENV UV_LINK_MODE=copy

# Copy dependency metadata first so source changes do not invalidate the dependency layer.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra otel --no-install-project

# Install the project separately so normal code changes reuse the cached dependency layer.
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install .

# Stage 2: Runtime image
FROM python:3.11-slim AS runtime

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Set environment
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# Install curl for HEALTHCHECK
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Create data directory
RUN mkdir -p /data/knowledge /data/index /data/chroma /data/graph

# Default environment variables
ENV LITHOS_DATA_DIR=/data
ENV LITHOS_HOST=0.0.0.0
ENV LITHOS_PORT=8765

# Expose SSE port
EXPOSE 8765

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8765/health || exit 1

# Default command: run SSE server
CMD ["python", "-m", "lithos.cli", "serve", "--transport", "sse", "--host", "0.0.0.0", "--port", "8765"]
