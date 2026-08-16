.PHONY: init deps create-db migrate migration-status load fetch pipeline health \
        test test-shell test-unit test-integration test-python test-schema-idempotency \
        test-load-integration test-container test-deploy check-python-deps \
        docker-build compose-up compose-down lint fmt \
        lint-python format-python format-python-check typecheck lint-sql quality \
        local-db-up local-db-migrate local-db-ready local-db-status \
        local-db-shell local-db-logs local-db-down local-db-reset \
        test-compose-integration test-ci-complete-run

# Requires uv (https://docs.astral.sh/uv/). Creates/updates .venv from the
# committed uv.lock (exact, hash-locked versions -- no ad hoc `pip install`)
# for the FULL locked toolchain (runtime deps + Ruff/mypy/pytest/SQLFluff/
# pre-commit -- see pyproject.toml's [dependency-groups] dev and README.md's
# "Python development and quality tooling" section) and installs the
# pre-commit git hook via that same locked environment.
init:
	uv sync --locked
	uv run pre-commit install
	@echo "Copying env template -> .env (edit it!)"
	cp -n scripts/setup_env.example .env || true

# Alias for `uv sync --locked` -- installs the tuva_postgres package and its
# runtime dependencies (requests, psycopg) from the committed uv.lock. Named
# separately from `init` so CI/container steps that only need dependencies
# (not the pre-commit git hook or a local .env copy) have a minimal target.
deps:
	uv sync --locked

create-db:
	. .env && bash scripts/apply_schema.sh

# Equivalent to `create-db` (both delegate to the same authoritative
# migration runner, src/tuva_postgres/migrations.py -- see
# scripts/apply_schema.sh) but invoked directly through the CLI. Prefer
# whichever fits your workflow; they can be used interchangeably.
migrate:
	. .env && uv run tuva-postgres migrate

# Read-only: prints applied/pending migrations and any checksum mismatches
# without applying anything or taking the migration advisory lock for
# longer than the read itself.
migration-status:
	. .env && uv run tuva-postgres migrate --status

load:
	. .env && bash scripts/load_to_postgres.sh

# Fetches + validates the manifest and publishes a raw snapshot only (no
# migrate/load/test). Requires TUVA_API_MANIFEST_URL/TUVA_API_TOKEN/
# RAW_DATA_DIR (see .env / scripts/setup_env.example).
fetch:
	. .env && uv run tuva-postgres fetch

# Runs the full production pipeline once: fetch -> migrate -> load -> test
# (src/tuva_postgres/orchestrator.py). Requires the full env (see
# scripts/setup_env.example) and a real (or locally-mocked, see
# tests/integration) manifest endpoint.
pipeline:
	. .env && uv run tuva-postgres run

# DB connectivity + migration state + last-successful-run freshness.
# Requires PG_DSN; safe to run anywhere, does not mutate anything.
health:
	. .env && uv run tuva-postgres healthcheck

test-shell:
	bash scripts/tests/test_apply_schema_terminology_path.sh
	bash scripts/tests/test_run_tests_path.sh
	bash scripts/tests/test_run_tests_no_embedded_workflow.sh
	bash scripts/tests/test_load_to_postgres_no_legacy_seed.sh
	bash scripts/tests/test_load_to_postgres_atomic.sh
	bash scripts/tests/test_patient_gender_index.sh
	bash scripts/tests/test_no_legacy_root_sql_files.sh
	bash scripts/tests/test_versioned_migration_layout.sh
	bash scripts/tests/test_migration_execution_modes.sh
	bash scripts/tests/test_schema_idempotency_harness_controls.sh
	bash scripts/tests/test_sql_test_layout.sh
	bash scripts/tests/test_schema_identifier_validation.sh
	python3 scripts/tests/test_constraint_idempotency_guards.py
	python3 scripts/tests/test_pre_commit_config.py
	python3 scripts/tests/test_python_dependencies.py
	python3 scripts/tests/test_ci_fixture.py
	python3 scripts/tests/test_no_raw_schema_interpolation.py

# Proves the real migration runner (scripts/apply_schema.sh) is idempotent:
# applies every discovered migration once into uniquely-named temporary
# schemas, then invokes the same entry point a second time and asserts a
# true no-op -- zero migrations applied, the complete migration-history
# table unchanged, and a deterministic catalog fingerprint unchanged.
# Never hand-reruns migration SQL; never touches "tuva"/"tuva_term"/
# "tuva_ops". Run by CI on every push/pull_request/schedule; kept outside
# test-shell/test because it requires a real, DISPOSABLE PostgreSQL test
# database via PG_DSN (see .env).
test-schema-idempotency:
	. .env && bash scripts/tests/test_schema_constraint_idempotency.sh

# Requires a real, DISPOSABLE PostgreSQL test database via PG_DSN (see .env).
# Loads a complete CSV snapshot into a uniquely-named temporary schema
# twice (proving retries don't duplicate rows), then loads an invalid
# snapshot and confirms the prior snapshot survives intact. Creates and
# drops its own temporary schema. Never run against production.
test-load-integration:
	. .env && bash scripts/tests/test_load_to_postgres_atomic_integration.sh

# The canonical, committed, deterministic complete CSV snapshot used ONLY
# for the database-backed CI smoke run below -- never the developer
# data/ directory (see tests/fixtures/ci/complete_snapshot/'s own
# README.md section and scripts/tests/test_ci_fixture.py). RUN_ID
# defaults to a locally-generated timestamped id for ad hoc local runs;
# CI supplies its own deterministic id (see .github/workflows/ci.yml) by
# exporting RUN_ID before calling this target, which `?=` never
# overrides.
CI_FIXTURE_DIR := tests/fixtures/ci/complete_snapshot
RUN_ID ?= local-complete-run-$(shell date -u +%Y%m%dt%H%M%Sz)

# Requires a real, DISPOSABLE PostgreSQL database (PG_DSN/PG_SCHEMA/... --
# from .env, or already exported by the caller, e.g. CI). Runs the real
# migrate -> load -> test -> verify sequence against the CONFIGURED
# database/schema (never a throwaway schema of its own -- for that, see
# `bash scripts/tests/test_ci_complete_run.sh` instead, which is safe to
# run against any disposable database regardless of what PG_SCHEMA is
# set to): applies migrations, prints migration status, loads the
# committed fixture above through the real scripts/load_to_postgres.sh
# (never data/), runs the real scripts/run_tests.sh SQL validation suite
# with this run's RUN_ID, then calls scripts/verify_complete_run.py to
# prove the run was actually complete (expected row counts, results tied
# to this exact RUN_ID, every expected suite represented, zero failures,
# migration status current) rather than just "the previous steps exited
# zero". Exits nonzero on any failure. NEVER run against production.
test-ci-complete-run:
	. .env && uv run tuva-postgres migrate
	. .env && uv run tuva-postgres migrate --status
	. .env && DATA_DIR=$(CI_FIXTURE_DIR) bash scripts/load_to_postgres.sh
	. .env && RUN_ID=$(RUN_ID) bash scripts/run_tests.sh
	. .env && RUN_ID=$(RUN_ID) uv run python3 scripts/verify_complete_run.py --fixture-dir $(CI_FIXTURE_DIR)
	@echo "test-ci-complete-run: migrate, load (committed fixture), SQL validation, and verification all passed (RUN_ID=$(RUN_ID))."

# Python unit tests for the src/tuva_postgres package (tests/unit/), run
# through pytest (see [tool.pytest.ini_options] in pyproject.toml --
# pytest collects and runs these unittest.TestCase suites natively, no
# rewrite required). No database, Docker, or network required --
# DB-touching code paths are exercised against fakes/an in-process mock
# HTTP server (see tests/unit/test_orchestrator.py's module docstring).
# Safe to run anywhere `uv sync --locked` has been run. Scoped to
# tests/unit ONLY -- never tests/integration -- so a plain unit-test run
# can never accidentally depend on, or connect to, a database.
test-unit:
	uv run pytest tests/unit

# Requires a real, DISPOSABLE PostgreSQL test database via PG_DSN (see
# .env). Applies migrations, runs the full pipeline twice against an
# in-process mock manifest server, and injects a corrupt artifact -- see
# tests/integration/test_pipeline_integration.py's module docstring for
# exactly what this proves. Creates and drops its own uniquely-suffixed
# schemas only. Never run against production. Run through pytest, scoped
# to tests/integration ONLY (marked `integration` in pyproject.toml's
# pytest markers, for anyone filtering with `-m`).
test-integration:
	. .env && uv run pytest tests/integration

# The complete pytest suite in one command, still database-free by
# default: collects both tests/unit and tests/integration but deselects
# anything marked `integration` (see [tool.pytest.ini_options] in
# pyproject.toml), so this is safe to run without PG_DSN/a database --
# it's the pytest-native equivalent of `test-unit`, useful for confirming
# collection across the whole tests/ tree (import errors, marker typos,
# duplicate test IDs) in one pass. Use `test-integration` (above) to
# actually exercise the database-dependent suite.
test-python:
	uv run pytest tests -m "not integration"

test: test-shell
	. .env && bash scripts/run_tests.sh

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

# Applies Ruff's formatter in place. NEVER run this against db/ -- Ruff
# only touches *.py files, so SQL is never at risk from this target, but
# it's still meant for src/tests/scripts, not a blanket repo-wide format.
format-python:
	uv run ruff format src tests scripts

# Same as format-python, but fails instead of rewriting -- what CI and
# the pre-commit ruff-format-check hook actually run.
format-python-check:
	uv run ruff format --check src tests scripts

# Static type checking for the production package only (see [tool.mypy]'s
# `files = ["src/tuva_postgres"]` in pyproject.toml) -- must match what
# CI and the pre-commit mypy hook check. Test/script code is not held to
# the same typing bar.
typecheck:
	uv run mypy src/tuva_postgres

# SQLFluff lint (read-only) through the psql-aware wrapper, against every
# tracked *.sql file (migrations under db/migrations/ included -- see the
# wrapper's psql-variable normalization, which lets checksum-protected
# migration SQL lint cleanly without ever touching its actual content;
# and db/tests/, which is linted the same way). Distinct from `fmt`
# below, which is fix/format and manual-only.
lint-sql:
	uv run bash scripts/sqlfluff_psql_wrapper.sh lint $$(git ls-files '*.sql')

# Database-free quality gate: dependency-lock validation, Ruff lint, Ruff
# format check, mypy, the unit test suite, and SQLFluff lint. Does not
# require PG_DSN, Docker, or a running Postgres. This is what a developer
# (and CI) should run before every push.
quality: check-python-deps lint-python format-python-check typecheck test-unit lint-sql
	@echo "quality: dependency lock, Ruff (lint + format check), mypy, pytest unit suite, and SQLFluff lint all passed."

# The local Ruff/mypy/SQLFluff-lint hooks use `language: system` with
# their entry commands prefixed `uv run ...` directly (see
# .pre-commit-config.yaml), so every local hook resolves the locked .venv
# regardless of how pre-commit itself was invoked (via `make lint` below,
# or via the installed git hook triggered directly by `git commit`) --
# the committed uv.lock remains the sole tool-version source either way.
lint:
	uv run pre-commit run --all-files

# Ruff formatting (rewrites src/tests/scripts/*.py in place) plus the
# existing manual-only SQLFluff formatter/fixer.
#
# WARNING: db/migrations/ SQL is checksum-protected (see
# src/tuva_postgres/migrations.py) -- the sqlfluff-psql-fix hook only
# ever PREVIEWS fixes to stdout (see scripts/sqlfluff_psql_wrapper.sh's
# "format" mode; it never writes back to any file), but you must still
# never manually copy its suggested output back into an already-applied
# migration file. New, not-yet-applied SQL (a new migration, or anything
# under db/tests/) is the safe place to actually apply suggested fixes.
fmt:
	uv run ruff format src tests scripts
	uv run pre-commit run --hook-stage manual sqlfluff-psql-fix --all-files

# Structural checks on Dockerfile/.dockerignore/compose.yaml (always run,
# no Docker daemon required) plus a real `docker build` + container
# healthcheck smoke test, and a `docker compose config` render check,
# when `docker` (and the `compose` plugin) are available. Prints an
# explicit skip reason -- never a silent pass -- when they are not. This
# target only *renders* the Compose config (no containers are started);
# for a full, isolated Compose runtime smoke test (Postgres actually
# starting, migrating, and answering queries), see `make
# test-compose-integration`.
test-container:
	uv run python3 -m unittest tests.unit.test_container_structure -v
	@if command -v docker >/dev/null 2>&1; then \
		echo "docker found: building tuva-postgres:ci-smoke and running a healthcheck smoke test"; \
		docker build -t tuva-postgres:ci-smoke . && \
		docker run --rm tuva-postgres:ci-smoke --version; \
		if docker compose version >/dev/null 2>&1; then \
			echo "docker compose found: validating compose.yaml renders (docker compose config)"; \
			docker compose config > /dev/null && echo "docker compose config: OK (no containers started)"; \
		else \
			echo "SKIPPED: docker compose (v2 plugin) is not available -- compose.yaml structural checks"; \
			echo "         passed above (test_local_postgres_compose.py), but 'docker compose config' was"; \
			echo "         not attempted. Run 'make test-compose-integration' once it is available."; \
		fi; \
	else \
		echo "SKIPPED: docker is not available in this environment -- container structural checks"; \
		echo "         passed above, but a real 'docker build'/'docker compose config' was not attempted."; \
		echo "         See the final validation report for this project's own record of this skip. Run"; \
		echo "         'make test-compose-integration' separately once Docker is available for a full,"; \
		echo "         isolated Compose runtime smoke test."; \
	fi

# Structural checks on deploy/kubernetes/*.yaml (always run, no kubectl
# required) plus `kubectl kustomize` + `kubectl apply --dry-run=client`
# when kubectl is available. Prints an explicit skip reason -- never a
# silent pass -- when it is not. Never applies anything for real.
test-deploy:
	uv run python3 -m unittest tests.unit.test_kubernetes_structure -v
	@if command -v kubectl >/dev/null 2>&1; then \
		echo "kubectl found: rendering and dry-run validating deploy/kubernetes"; \
		kubectl kustomize deploy/kubernetes | kubectl apply --dry-run=client -f -; \
	else \
		echo "SKIPPED: kubectl is not available in this environment -- manifest structural checks"; \
		echo "         passed above, but 'kubectl kustomize'/'--dry-run=client' were not attempted."; \
	fi

docker-build:
	docker build -t tuva-postgres:local .

# Pre-existing, general-purpose Compose targets (build + start everything
# in compose.yaml, or tear it down). Prefer the more specific `local-db-*`
# targets below for the day-to-day "just the database" workflow -- these
# two remain for whole-stack (postgres + migrate + pipeline) usage.
#
# compose-down intentionally does NOT pass `-v`: routine shutdown must
# never delete the local Postgres data volume. `local-db-reset` (or
# `docker compose down -v` run deliberately by hand) is the only
# supported way to delete local database data.
compose-up:
	docker compose up --build -d

compose-down:
	docker compose down

# --- Local PostgreSQL lifecycle (see README.md's "Local PostgreSQL with
#     Docker Compose" section for the full walkthrough) --------------------
#
# HOST_DSN mirrors compose.yaml's local-only credentials exactly (see
# scripts/setup_local_postgres.example) -- this is a clearly-labeled,
# local-development-only placeholder, never a production secret, so it
# is safe to print.
HOST_PG_PORT ?= $(if $(POSTGRES_PORT),$(POSTGRES_PORT),5432)
HOST_DSN := postgresql://tuva_local:local-only-example-password-change-me@127.0.0.1:$(HOST_PG_PORT)/tuva

# Starts just Postgres, detached, and blocks until Compose reports it
# healthy (`--wait`, Docker Compose v2). Never touches the data volume;
# safe to run against an already-running or already-populated database.
# Never requires TUVA_API_MANIFEST_URL/TUVA_API_TOKEN.
local-db-up:
	docker compose up -d --wait --wait-timeout 60 postgres
	@echo "postgres is healthy (host port $(HOST_PG_PORT))."

# Applies the repository's real, checksum-protected migrations
# (tuva-postgres migrate, via the one-shot `migrate` Compose service)
# against the local database over the Compose network. Exits nonzero on
# migration failure (checksum mismatch, execution-mode mismatch, etc. --
# see src/tuva_postgres/migrations.py). Idempotent: already-applied
# migrations are skipped, so this is always safe to rerun.
local-db-migrate:
	docker compose run --rm migrate

# One command from a stopped stack to a healthy, migrated local database.
local-db-ready: local-db-up local-db-migrate
	@echo ""
	@echo "Local Postgres is up, healthy, and migrated."
	@echo "Host DSN: $(HOST_DSN)"
	@echo "Load it into your shell with: cp scripts/setup_local_postgres.example .env && . .env"

# Read-only: container state + migration status. Never mutates the
# database (uses `tuva-postgres migrate --status`, see
# src/tuva_postgres/migrations.py's status()).
local-db-status:
	docker compose ps postgres migrate pipeline
	@echo ""
	@echo "Migration status:"
	docker compose run --rm migrate migrate --status

# Opens psql against the local database using Postgres's own installed
# client inside the running `postgres` container (no host `psql`
# required). Requires `local-db-up`/`local-db-ready` to already be
# running -- this uses `exec`, not `run`, so it attaches to the real,
# already-healthy database rather than starting a throwaway one.
local-db-shell:
	docker compose exec postgres psql -U tuva_local -d tuva

# Follows Postgres's logs (Ctrl-C to stop following; does not stop the
# container).
local-db-logs:
	docker compose logs -f postgres

# Stops and removes the local stack's containers/network. Preserves the
# named data volume -- local database data survives an ordinary
# stop/start cycle. Never passes `-v`.
local-db-down:
	docker compose down

# DESTRUCTIVE: deletes this Compose project's local volumes, including
# the Postgres data volume -- ALL local database data is permanently
# lost. Requires an explicit opt-in: either run with
# `CONFIRM_LOCAL_DB_RESET=yes` (safe for noninteractive automation, e.g.
# a personal cleanup script) or answer the interactive confirmation
# prompt. Only ever targets this Compose project's own resources (never
# `docker system prune`, never another project's volumes).
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

# Runs the isolated, self-contained Docker Compose runtime smoke test
# (scripts/tests/test_local_postgres_compose.sh) against a uniquely
# named, disposable Compose project -- never the developer's own
# `local-db-*` stack. Requires Docker; prints a clear SKIPPED message and
# exits successfully if Docker/Compose are unavailable.
test-compose-integration:
	bash scripts/tests/test_local_postgres_compose.sh
