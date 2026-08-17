# syntax=docker/dockerfile:1
#
# Application image for the tuva-ingest connector (src/tuva_ingest) plus
# the dbt project that maps raw tables into the Tuva Input Layer
# (dbt_project.yml, models/, packages.yml -- pinned to
# tuva-health/the_tuva_project 0.18.0). Two stages:
#   1. `builder` resolves the locked dependency set (uv.lock) -- which
#      already includes dbt-core/dbt-postgres, see pyproject.toml -- into
#      a self-contained virtualenv. Nothing from this stage except that
#      venv makes it into the final image.
#   2. `runtime` copies the locked venv, the connector source, and the
#      dbt project, and runs as a non-root user. `dbt deps` is NOT run
#      at build time (it needs network access to fetch the pinned Tuva
#      package and is a deliberate, explicit step -- see compose.yml's
#      `dbt-deps` service / README.md) so a rebuilt image always uses
#      whatever package version compose/CI resolves against packages.yml.
#
# Build:   docker build -t tuva-ingest:local .
# Extract: docker run --rm --env-file .env tuva-ingest:local extract --endpoint medical-claims
# Health:  docker run --rm --env-file .env tuva-ingest:local healthcheck

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

LABEL org.opencontainers.image.title="tuva-ingest" \
      org.opencontainers.image.description="Raw-to-Input-Layer ingestion connector for the Tuva Project dbt package (0.18.0)" \
      org.opencontainers.image.source="https://example.invalid/tuva-postgres" \
      org.opencontainers.image.licenses="MIT"

# git is required by `dbt deps` to resolve the pinned Tuva package
# (packages.yml); postgresql-client (psql) is kept for operator
# debugging only -- the connector itself talks to PostgreSQL exclusively
# through psycopg (src/tuva_ingest/db.py), never by shelling out to psql.
RUN apt-get update \
    && apt-get install --no-install-recommends -y git postgresql-client \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 tuva \
    && useradd --system --uid 10001 --gid tuva --home-dir /app --shell /usr/sbin/nologin tuva

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --chown=tuva:tuva src ./src
COPY --chown=tuva:tuva migrations ./migrations
COPY --chown=tuva:tuva models ./models
COPY --chown=tuva:tuva macros ./macros
COPY --chown=tuva:tuva dbt_project.yml packages.yml profiles.example.yml ./
# profiles.example.yml is entirely env-var-driven with safe local
# placeholder defaults (no real credentials -- see the file's own
# header comment), so it doubles as the shipped profiles.yml: a
# container operator overriding PGHOST/PGUSER/PGPASSWORD/etc. (or
# DBT_PROFILES_DIR to mount a custom profiles.yml) gets a real profile
# without this image ever baking in a secret.
RUN cp profiles.example.yml profiles.yml
COPY --chown=tuva:tuva pyproject.toml uv.lock ./

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RAW_DATA_DIR=/app/data/raw \
    DBT_PROJECT_DIR=/app \
    DBT_PROFILES_DIR=/app

# The only locations the app is expected to write to at runtime (raw
# snapshots, dbt's own logs/target dirs) -- everything else under /app
# can be mounted read-only in production.
RUN mkdir -p /app/data/raw /app/tmp /app/logs /app/target /app/dbt_packages \
    && chown -R tuva:tuva /app/data /app/tmp /app/logs /app/target /app/dbt_packages

USER tuva

HEALTHCHECK --interval=5m --timeout=30s --start-period=30s --retries=3 \
    CMD ["tuva-ingest", "healthcheck"]

# Exec form: PID 1 is `tuva-ingest` itself, so SIGTERM from `docker stop`
# is delivered directly to the CLI process, not swallowed by a shell.
ENTRYPOINT ["tuva-ingest"]
CMD ["run"]
