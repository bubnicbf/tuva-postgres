.PHONY: init deps create-db migrate migration-status load fetch pipeline health \
        test test-shell test-unit test-integration test-schema-idempotency \
        test-load-integration test-container test-deploy check-python-deps \
        docker-build compose-up compose-down lint fmt \
        local-db-up local-db-migrate local-db-ready local-db-status \
        local-db-shell local-db-logs local-db-down local-db-reset \
        test-compose-integration

# Requires uv (https://docs.astral.sh/uv/). Creates/updates .venv from the
# committed uv.lock (exact, hash-locked versions -- no ad hoc `pip install`)
# and installs the pre-commit git hook via that same locked environment.
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
	bash scripts/tests/test_sql_test_layout.sh
	python3 scripts/tests/test_constraint_idempotency_guards.py
	python3 scripts/tests/test_pre_commit_config.py
	python3 scripts/tests/test_python_dependencies.py

# Requires a real, DISPOSABLE PostgreSQL test database via PG_DSN (see .env).
# Applies the core table DDL twice into a uniquely-named temporary schema
# that it creates and drops itself. Never run against production.
test-schema-idempotency:
	. .env && bash scripts/tests/test_schema_constraint_idempotency.sh

# Requires a real, DISPOSABLE PostgreSQL test database via PG_DSN (see .env).
# Loads a complete CSV snapshot into a uniquely-named temporary schema
# twice (proving retries don't duplicate rows), then loads an invalid
# snapshot and confirms the prior snapshot survives intact. Creates and
# drops its own temporary schema. Never run against production.
test-load-integration:
	. .env && bash scripts/tests/test_load_to_postgres_atomic_integration.sh

# Python unit tests for the src/tuva_postgres package (tests/unit/). No
# database, Docker, or network required -- DB-touching code paths are
# exercised against fakes/an in-process mock HTTP server (see
# tests/unit/test_orchestrator.py's module docstring). Safe to run
# anywhere `uv sync` has been run.
test-unit:
	uv run python3 -m unittest discover -s tests/unit -v

# Requires a real, DISPOSABLE PostgreSQL test database via PG_DSN (see
# .env). Applies migrations, runs the full pipeline twice against an
# in-process mock manifest server, and injects a corrupt artifact -- see
# tests/integration/test_pipeline_integration.py's module docstring for
# exactly what this proves. Creates and drops its own uniquely-suffixed
# schemas only. Never run against production.
test-integration:
	. .env && uv run python3 -m unittest discover -s tests/integration -v

test: test-shell
	. .env && bash scripts/run_tests.sh

# Verifies the locked Python toolchain is current and actually installs,
# without mutating anything database-related. Safe to run anywhere uv is
# available; does not require PG_DSN or a running Postgres.
check-python-deps:
	uv lock --check
	uv sync --locked
	uv run sqlfluff --version
	uv run pre-commit --version

# The local sqlfluff-psql-fix hook uses `language: system`, so it relies on
# whatever `sqlfluff` is first on PATH -- `uv run` puts the locked .venv on
# PATH for the duration of the command, which is what makes pre-commit find
# the locked SQLFluff install here instead of whatever (if anything) is on
# the system PATH.
lint:
	uv run pre-commit run --all-files

# Runs the manual-only sqlfluff-psql-fix hook (see .pre-commit-config.yaml)
# against every file it's scoped to, via the same locked environment.
fmt:
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
