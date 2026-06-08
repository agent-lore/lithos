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

# Bake the default sentence-transformers embedding model into the image so the
# runtime never downloads it from HuggingFace at startup. Container startup was
# failing in CI when HF rate-limited (HTTP 429) the shared runner IPs; baking
# the model makes startup network-free and fast. The spaCy NER model already
# ships as a wheel dependency, so only the embedding model needs this.
#
# The download is retried with backoff: HuggingFace is intermittently
# unreachable from shared CI IPs, and without retries a transient outage fails
# the whole image build (and the PR's CI).
ENV HF_HOME=/opt/hf-cache
RUN for attempt in 1 2 3 4 5; do \
        python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')" && exit 0; \
        echo "HuggingFace model download attempt ${attempt} failed; retrying in $((attempt * 10))s"; \
        sleep $((attempt * 10)); \
    done; \
    echo "ERROR: HuggingFace model download failed after 5 attempts" >&2; \
    exit 1

# Stage 2: Runtime image
FROM python:3.11-slim AS runtime

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy the pre-baked embedding model cache from the builder.
COPY --from=builder /opt/hf-cache /opt/hf-cache

# Set environment
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
# Load the embedding model from the baked cache and never reach out to
# HuggingFace at runtime. Override HF_HUB_OFFLINE=0 if you configure a
# non-default embedding model that must be downloaded.
ENV HF_HOME=/opt/hf-cache
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1

# Install curl for HEALTHCHECK
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Create runtime user and data directory with host-friendly ownership
RUN groupadd --gid 1000 lithos \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/lithos --shell /usr/sbin/nologin lithos \
    && mkdir -p /data/knowledge /data/index /data/chroma /data/graph \
    && chown -R 1000:1000 /data /home/lithos /opt/hf-cache

# Default environment variables
ENV HOME=/home/lithos
ENV LITHOS_DATA_DIR=/data
ENV LITHOS_HOST=0.0.0.0
ENV LITHOS_PORT=8765

# Expose SSE port
EXPOSE 8765

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8765/health || exit 1

USER 1000:1000

# Default command: run SSE server
CMD ["python", "-m", "lithos.cli", "serve", "--transport", "sse", "--host", "0.0.0.0", "--port", "8765"]
