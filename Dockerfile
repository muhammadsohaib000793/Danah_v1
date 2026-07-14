# ============================================================================
# DANAH — API / worker / scheduler image
# One image, three entrypoints (selected by docker-compose `command`).
# ============================================================================
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libpq / lxml build inputs; curl for the container healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libxml2-dev \
        libxslt1-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/danah

# Dependency layer — cached until pyproject changes.
COPY pyproject.toml README.md ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -e ".[dev]"

# Application layer.
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts
COPY tests ./tests
COPY web ./web

# Document storage volume mount point.
RUN mkdir -p /srv/danah/data/documents

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 danah \
    && chown -R danah:danah /srv/danah
USER danah

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=5 \
    CMD curl -fsS http://localhost:8000/api/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
