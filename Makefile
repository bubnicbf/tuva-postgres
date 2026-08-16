.PHONY: init deps migrate migrate-status extract load-raw run health \
        dbt-debug dbt-deps dbt-parse dbt-compile dbt-build dbt-test \
        dbt-input-layer dbt-dq-structural dbt-dq-logical dbt-dq-analytical \
        test-unit test-integration test-python check-python-deps \
        lint-python format-python format-python-check typecheck lint-sql \
        quality lint fmt pipeline \
        docker-build test-container \
        compose-up compose-down \
        local-db-up local-db-migrate local-db-ready local-db-status \
        local-db-shell local-db-logs local-db-down local-db-reset \
        ci-full

# Requires uv (https://docs.astral.sh/uv/). Creates/updates .venv from the
# committed uv.lock (exact, hash-locked versions -- no ad hoc `pip
# install`) for the FULL locked toolchain: runtime deps (requests,
# psycopg, dbt-core, dbt-postgres) plus Ruff/mypy/pytest/SQLFluff/
# pre-commit (see pyproject.toml's [dependency-groups] dev and README.md's
# "Python development and quality tooling" section), and installs the
# pre-commit git hook via that same locked environment.
init:
	uv sync --locked
	uv run pre-commit install
	@echo "Copying env template -> .env (edit it!)"
	cp -n scripts/setup_env.example .env || true

# Alias for `uv sync --locked` -- installs the tuva_ingest package and its
# runtime dependencies from the committed uv.lock. Named separately from
# `init` so CI/container steps that only need dependencies (not the
# pre-commit git hook or a local .env copy) have a minimal target.
deps:
	uv sync --locked

# Applies pending migrations/001-003 (see src/tuva_ingest/migrations.py).
# Never creates or touches any Tuva-managed core/terminology/output
# schema -- only the configured raw and operational-control schemas.
# Idempotent and safe to rerun.
migrate:
	. .env && uv run tuva-ingest migrate

# Read-only: prints applied/pending migrations and any checksum
# mismatches without applying anything or holding the migration advisory
# lock for longer than the read itself.
migrate-status:
	. .env && uv run tuva-ingest migrate --status

# Fetches + validates the manifest and publishes a raw snapshot only (no
# load-raw/dbt). Requires TUVA_API_MANIFEST_URL/TUVA_API_TOKEN/
# RAW_DATA_DIR (see .env / scripts/setup_env.example).
extract:
	. .env && uv run tuva-ingest extract

# Loads the current (or --snapshot-id) published raw snapshot into the
# configured raw schema only. Requires PG_DSN and RAW_DATA_DIR.
load-raw:
	. .env && uv run tuva-ingest load-raw

# Checks the dbt profile/connection (PG_DSN, target config) resolves
# and can actually reach the configured PostgreSQL database. The first
# step of the structural-DQ-gated pipeline (see README.md "Validation
# order") -- run before dbt-deps so a bad connection fails fast.
dbt-debug:
	. .env && uv run tuva-ingest dbt -- debug

# Fetches the pinned Tuva package (packages.yml, tuva-health/
# the_tuva_project == 0.18.0) plus dbt_utils. Requires network access to
# dbt Hub.
dbt-deps:
	. .env && uv run tuva-ingest dbt -- deps

# Parses the dbt project without connecting to a database -- catches
# Jinja/YAML/ref() errors fast, in CI or locally.
dbt-parse:
	. .env && uv run tuva-ingest dbt -- parse

# Compiles the dbt project (requires a database connection for
# information-schema introspection, but does not build or test anything).
dbt-compile:
	. .env && uv run tuva-ingest dbt -- compile

# Builds staging -> Input Layer -> the pinned Tuva package's own models
# against the currently loaded raw schema, and runs every configured
# dbt test. Requires PG_DSN, RAW_SCHEMA/INPUT_LAYER_SCHEMA populated
# (via load-raw), and dbt-deps to have already succeeded.
dbt-build:
	. .env && uv run tuva-ingest dbt -- build

dbt-test:
	. .env && uv run tuva-ingest dbt -- test

# --- Structural DQ gate (see README.md "Validation order" and
#     "Structural DQ must pass before logical/analytical DQ") ---------
#
# Stage 1: build ONLY this connector's own models (models/staging/*.sql
# + models/final/*.sql, both tagged `input_layer` in dbt_project.yml --
# see that file's own comment for why staging is tagged too, not just
# final) plus their schema tests (models/staging/schema.yml,
# models/final/schema.yml). This must pass before the pinned Tuva
# package's own models are built at all.
dbt-input-layer:
	. .env && uv run tuva-ingest dbt -- build --select tag:input_layer

# Stage 2: the pinned Tuva package's own structural data-quality
# checks -- the package-provided tests that validate the Input Layer's
# STRUCTURE (required models exist, required columns exist with
# compatible types, primary keys hold) as opposed to the logical/
# analytical DQI checks that validate the VALUES within that structure.
# Requires dbt-input-layer to have already succeeded (a structural
# failure here means Tuva's own package cannot safely resolve this
# project's Input Layer models; never proceed to logical/analytical DQ
# or downstream marts when this fails -- see README.md).
#
# NOTE: this repository's sandboxed development/validation environment
# has no outbound network access to dbt Hub/PyPI and no local
# PostgreSQL, so `tag:dq_structural`'s exact node selection could not be
# executed and confirmed against the live pinned package here (see
# README.md "Known limitations" and the final PR description for the
# full record of what was, and was not, run). Before relying on this
# target in production, run it once in a network-enabled environment and
# confirm `dbt ls --select tag:dq_structural` selects a non-empty,
# expected set of nodes.
dbt-dq-structural:
	. .env && uv run tuva-ingest dbt -- build --select tag:dq_structural

# Stage 3 (optional): the pinned Tuva package's logical DQI checks, if/
# when this project selects them explicitly (see
# https://thetuvaproject.com/data-quality-overview). Never runs unless
# dbt-dq-structural has already succeeded.
dbt-dq-logical:
	. .env && uv run tuva-ingest dbt -- build --select tag:dq_logical

# Stage 4 (optional): the pinned Tuva package's analytical/metric-level
# DQI checks, if/when this project selects them explicitly. Never runs
# unless dbt-dq-logical has already succeeded.
dbt-dq-analytical:
	. .env && uv run tuva-ingest dbt -- build --select tag:dq_analytical

# Runs the full production pipeline once: migrate -> extract -> load-raw
# -> dbt deps -> dbt build (src/tuva_ingest/cli.py's `run` subcommand).
# Requires the full env (see scripts/setup_env.example) and a real (or
# locally-mocked, see tests/integration) manifest endpoint.
run:
	. .env && uv run tuva-ingest run

# DB connectivity + migration state + last-successful-run freshness.
# Requires PG_DSN; safe to run anywhere, does not mutate anything.
health:
	. .env && uv run tuva-ingest healthcheck

# Verifies the locked Python toolchain is current and actually installs,
# without mutating anything database-related. Safe to run anywhere uv is
# available; does not require PG_DSN or a running Postgres.
check-python-deps:
	uv lock --check
	uv sync --locked
	uv run ruff --version
	uv run mypy --version
	uv run pytest --version
	uv run sqlfluff --version
	uv run pre-commit --version

# Ruff lint, in check mode (no autofix) -- see [tool.ruff]/[tool.ruff.lint]
# in pyproject.toml for the configured rule set and per-file ignores.
lint-python:
	uv run ruff check src tests scripts

# Applies Ruff's formatter in place.
format-python:
	uv run ruff format src tests scripts

# Same as format-python, but fails instead of rewriting -- what CI and
# the pre-commit ruff-format-check hook actually run.
format-python-check:
	uv run ruff format --check src tests scripts

# Static type checking for the production package only (see [tool.mypy]'s
# `files = ["src/tuva_ingest"]` in pyproject.toml) -- must match what CI
# and the pre-commit mypy hook check.
typecheck:
	uv run mypy src/tuva_ingest

# SQLFluff lint (read-only) through the psql-aware wrapper (see
# scripts/sqlfluff_psql_wrapper.sh -- migrations/*.sql use psql-style
# :"name" variables, which are not valid standalone PostgreSQL syntax on
# their own), against every tracked *.sql file: migrations/ and
# models/**/*.sql alike (the wrapper only rewrites psql-var placeholders;
# dbt Jinja is handled natively by SQLFluff's jinja templater, see
# [tool.sqlfluff.templater.jinja] in pyproject.toml).
lint-sql:
	uv run bash scripts/sqlfluff_psql_wrapper.sh lint $$(git ls-files '*.sql')

# Python unit tests for the src/tuva_ingest package (tests/unit/), run
# through pytest (see [tool.pytest.ini_options] in pyproject.toml --
# pytest collects and runs these unittest.TestCase suites natively). No
# database, Docker, or network required -- DB-touching code paths are
# exercised against fakes or an in-process mock HTTP server (see
# tests/unit/test_api_client.py, tests/unit/test_state.py). Scoped to
# tests/unit ONLY -- never tests/integration -- so a plain unit-test run
# can never accidentally depend on, or connect to, a database.
test-unit:
	uv run pytest tests/unit

# Requires a real, DISPOSABLE PostgreSQL database via PG_DSN (see .env).
# Applies migrations, loads tests/fixtures/*.csv into a uniquely-suffixed
# raw schema twice (proving retries don't duplicate rows), corrupts one
# checksum and confirms the whole snapshot's transaction rolls back, and
# (when `dbt` is on PATH) runs a real `dbt build` proving raw fixtures ->
# staging -> Input Layer -> the pinned Tuva package's own models. Creates
# and drops only its own uniquely-suffixed schemas. Never run against
# production. See tests/integration/test_pipeline_integration.py's module
# docstring for the full list of what this proves.
test-integration:
	. .env && uv run pytest tests/integration

# The complete pytest suite in one command, still database-free by
# default: collects both tests/unit and tests/integration but deselects
# anything marked `integration` (see [tool.pytest.ini_options] in
# pyproject.toml). Use `test-integration` (above) to actually exercise
# the database-dependent suite.
test-python:
	uv run pytest tests -m "not integration"

# Database-free quality gate: dependency-lock validation, Ruff lint,
# Ruff format check, mypy, the unit test suite, and SQLFluff lint. Does
# not require PG_DSN, Docker, or a running Postgres. This is what a
# developer (and CI) should run before every push.
quality: check-python-deps lint-python format-python-check typecheck test-unit lint-sql
	@echo "quality: dependency lock, Ruff (lint + format check), mypy, pytest unit suite, and SQLFluff lint all passed."

# The local Ruff/mypy/SQLFluff-lint hooks use `language: system` with
# their entry commands prefixed `uv run ...` directly (see
# .pre-commit-config.yaml), so every local hook resolves the locked
# .venv regardless of how pre-commit itself was invoked.
lint:
	uv run pre-commit run --all-files

fmt:
	uv run ruff format src tests scripts

docker-build:
	docker build -t tuva-ingest:local .

# Structural build + healthcheck smoke test when `docker` is available.
# Prints an explicit skip reason -- never a silent pass -- when it is
# not.
test-container:
	@if command -v docker >/dev/null 2>&1; then \
		echo "docker found: building tuva-ingest:ci-smoke and running --version"; \
		docker build -t tuva-ingest:ci-smoke . && \
		docker run --rm tuva-ingest:ci-smoke --version; \
		if docker compose version >/dev/null 2>&1; then \
			echo "docker compose found: validating compose.yml renders (docker compose config)"; \
			docker compose config > /dev/null && echo "docker compose config: OK (no containers started)"; \
		else \
			echo "SKIPPED: docker compose (v2 plugin) is not available -- 'docker compose config' was not attempted."; \
		fi; \
	else \
		echo "SKIPPED: docker is not available in this environment -- a real 'docker build'/'docker compose config'"; \
		echo "         was not attempted. See the final validation report for this project's own record of this skip."; \
	fi

# Pre-existing, general-purpose Compose targets (build + start everything
# in compose.yml, or tear it down). Prefer the more specific `local-db-*`
# targets below for the day-to-day "just the database" workflow.
#
# compose-down intentionally does NOT pass `-v`: routine shutdown must
# never delete the local Postgres data volume. `local-db-reset` (or
# `docker compose down -v` run deliberately by hand) is the only
# supported way to delete local database data.
compose-up:
	docker compose up --build -d

compose-down:
	docker compose down

# --- Local PostgreSQL lifecycle (see README.md's "Local development"
#     section for the full walkthrough) --------------------------------
#
# HOST_DSN mirrors compose.yml's local-only credentials exactly -- a
# clearly-labeled, local-development-only placeholder, never a
# production secret, so it is safe to print.
HOST_PG_PORT ?= $(if $(POSTGRES_PORT),$(POSTGRES_PORT),5432)
HOST_DSN := postgresql://tuva_local:local-only-example-password-change-me@127.0.0.1:$(HOST_PG_PORT)/tuva

# Starts just Postgres, detached, and blocks until Compose reports it
# healthy (`--wait`, Docker Compose v2). Never touches the data volume.
local-db-up:
	docker compose up -d --wait --wait-timeout 60 postgres
	@echo "postgres is healthy (host port $(HOST_PG_PORT))."

# Applies the repository's real, checksum-protected migrations
# (tuva-ingest migrate, via the one-shot `migrate` Compose service)
# against the local database over the Compose network. Idempotent:
# already-applied migrations are skipped, so this is always safe to
# rerun.
local-db-migrate:
	docker compose run --rm migrate

# One command from a stopped stack to a healthy, migrated local database.
local-db-ready: local-db-up local-db-migrate
	@echo ""
	@echo "Local Postgres is up, healthy, and migrated."
	@echo "Host DSN: $(HOST_DSN)"

# Read-only: container state + migration status. Never mutates the
# database.
local-db-status:
	docker compose ps postgres migrate ingest
	@echo ""
	@echo "Migration status:"
	docker compose run --rm migrate migrate --status

# Opens psql against the local database using Postgres's own installed
# client inside the running `postgres` container (no host `psql`
# required). Requires `local-db-up`/`local-db-ready` to already be
# running.
local-db-shell:
	docker compose exec postgres psql -U tuva_local -d tuva

# Follows Postgres's logs (Ctrl-C to stop following; does not stop the
# container).
local-db-logs:
	docker compose logs -f postgres

# Stops and removes the local stack's containers/network. Preserves the
# named data volume. Never passes `-v`.
local-db-down:
	docker compose down

# DESTRUCTIVE: deletes this Compose project's local volumes, including
# the Postgres data volume -- ALL local database data is permanently
# lost. Requires an explicit opt-in: either run with
# `CONFIRM_LOCAL_DB_RESET=yes` or answer the interactive confirmation
# prompt.
local-db-reset:
	@if [ "$(CONFIRM_LOCAL_DB_RESET)" != "yes" ]; then \
		echo "This will PERMANENTLY DELETE all local Postgres data (the pgdata volume)."; \
		read -r -p "Type 'yes' to continue, anything else to abort: " reply; \
		if [ "$$reply" != "yes" ]; then \
			echo "Aborted. No data was deleted."; \
			exit 1; \
		fi; \
	fi
	docker compose down -v
	@echo "Local Postgres data volume removed. Run 'make local-db-ready' to start fresh."

# The single, complete, disposable-database CI validation target: every
# database-free quality check, plus dbt deps/parse, plus the full
# database-backed integration suite (migrations, raw loader, state,
# and -- when dbt is on PATH -- a real dbt build). Requires PG_DSN
# pointed at a disposable PostgreSQL database (see .env / CI's postgres
# service) and network access (for dbt-deps' Tuva package fetch).
ci-full: quality dbt-parse
	. .env && uv run tuva-ingest dbt -- deps
	$(MAKE) test-integration
	@echo "ci-full: quality gate, dbt parse/deps, and the full disposable-database integration suite all passed."

# The complete, stop-on-first-failure validation pipeline in the exact
# order required by README.md's "Validation order" (structural DQ must
# gate logical/analytical DQ, which in turn gate any downstream Tuva
# models/marts). Each step is a separate `make` invocation via `&&`, so
# the whole chain stops at the first non-zero exit code -- never
# continuing to a later stage after an earlier one fails. Requires
# PG_DSN pointed at a disposable PostgreSQL database and network access
# (for dbt-deps' Tuva package fetch). dbt-dq-logical/dbt-dq-analytical
# are included only if this project ever selects those tags explicitly
# (see those targets' own comments); until then they are effectively a
# documented no-op extension point, never a silent skip of a real check.
pipeline: quality
	$(MAKE) dbt-debug
	$(MAKE) dbt-deps
	$(MAKE) dbt-parse
	$(MAKE) dbt-input-layer
	$(MAKE) dbt-dq-structural
	@echo "pipeline: quality gate, dbt debug/deps/parse, input_layer build, and structural DQ all passed, in that order."
