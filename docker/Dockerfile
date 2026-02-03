# Exogram - Local shared knowledge base for AI agents
# Multi-stage build for smaller image

# Stage 1: Build dependencies
FROM python:3.11-slim AS builder

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files and README (required by pyproject.toml)
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Create virtual environment and install dependencies
RUN uv venv /app/.venv
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Install the package (production only, no dev deps for smaller image)
RUN uv pip install -e .

# Stage 2: Runtime image
FROM python:3.11-slim AS runtime

# Install runtime dependencies (none needed currently)
RUN apt-get update && apt-get install -y --no-install-recommends     && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/pyproject.toml /app/
COPY --from=builder /app/README.md /app/

# Set environment
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# Create data directory
RUN mkdir -p /data/knowledge /data/index /data/chroma /data/graph

# Default environment variables
ENV EXOGRAM_DATA_DIR=/data
ENV EXOGRAM_HOST=0.0.0.0
ENV EXOGRAM_PORT=8765

# Expose SSE port
EXPOSE 8765

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3     CMD python -c "import socket; s=socket.socket(); s.connect(('localhost', 8765)); s.close()" || exit 1

# Default command: run SSE server
CMD ["python", "-m", "exogram.cli", "serve", "--transport", "sse", "--host", "0.0.0.0", "--port", "8765"]
