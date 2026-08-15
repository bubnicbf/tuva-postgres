# syntax=docker/dockerfile:1
#
# Production image for the tuva-postgres ingestion pipeline
# (src/tuva_postgres). Two stages:
#   1. `builder` resolves the locked runtime dependency set (uv.lock) into
#      a self-contained virtualenv -- nothing from this stage except that
#      venv makes it into the final image.
#   2. runtime installs only the `psql` client (needed by
#      scripts/load_to_postgres.sh and scripts/run_tests.sh, which the
#      orchestrator still shells out to -- see src/tuva_postgres/
#      orchestrator.py), copies the locked venv and the application code,
#      and runs as a non-root user.
#
# Build:   docker build -t tuva-postgres:local .
# Run:     docker run --rm --env-file .env tuva-postgres:local run
# Health:  docker run --rm --env-file .env tuva-postgres:local healthcheck

ARG PYTHON_VERSION=3.12.7

# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

# uv is only needed to build the venv; it never ships in the final image.
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Copy only what's needed to resolve+install dependencies first, so this
# (slow) layer is cached across code-only changes.
COPY pyproject.toml uv.lock ./
COPY src ./src

# --locked fails the build if uv.lock is out of date with pyproject.toml
# (see `make check-python-deps`) -- the container build is itself a check
# that the committed lockfile is trustworthy. --no-dev excludes
# sqlfluff/pre-commit (dev-only tooling, see [dependency-groups] dev in
# pyproject.toml) from the runtime image.
RUN uv sync --locked --no-dev

# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="tuva-postgres" \
      org.opencontainers.image.description="Tuva healthcare data ingestion pipeline: fetch, migrate, load, test" \
      org.opencontainers.image.source="https://example.invalid/tuva-postgres" \
      org.opencontainers.image.licenses="MIT"

# psql client only (no full postgresql-server); required by
# scripts/load_to_postgres.sh and scripts/run_tests.sh.
RUN apt-get update \
    && apt-get install --no-install-recommends -y postgresql-client \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 tuva \
    && useradd --system --uid 10001 --gid tuva --home-dir /app --shell /usr/sbin/nologin tuva

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --chown=tuva:tuva src ./src
COPY --chown=tuva:tuva db ./db
COPY --chown=tuva:tuva scripts ./scripts
COPY --chown=tuva:tuva pyproject.toml uv.lock ./

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RAW_DATA_DIR=/app/data/raw \
    METRICS_FILE=/app/data/metrics/tuva_postgres.prom

# The only two locations the app is expected to write to at runtime
# (raw snapshots, the Prometheus textfile) -- everything else under /app
# can be mounted read-only in production.
RUN mkdir -p /app/data/raw /app/data/metrics /app/tmp \
    && chown -R tuva:tuva /app/data /app/tmp

USER tuva

HEALTHCHECK --interval=5m --timeout=30s --start-period=30s --retries=3 \
    CMD ["tuva-postgres", "healthcheck"]

# Exec form: PID 1 is `tuva-postgres` itself, so SIGTERM from `docker stop`
# / a Kubernetes CronJob's terminationGracePeriod is delivered directly to
# the process the orchestrator's signal guard handles (see
# src/tuva_postgres/orchestrator.py's _SignalGuard) -- no shell in between
# to swallow it.
ENTRYPOINT ["tuva-postgres"]
CMD ["run"]
