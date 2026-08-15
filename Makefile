.PHONY: init create-db load test test-shell test-schema-idempotency test-load-integration check-python-deps lint fmt

# Requires uv (https://docs.astral.sh/uv/). Creates/updates .venv from the
# committed uv.lock (exact, hash-locked versions -- no ad hoc `pip install`)
# and installs the pre-commit git hook via that same locked environment.
init:
	uv sync --locked
	uv run pre-commit install
	@echo "Copying env template -> .env (edit it!)"
	cp -n scripts/setup_env.example .env || true

create-db:
	. .env && bash scripts/apply_schema.sh

load:
	. .env && bash scripts/load_to_postgres.sh

test-shell:
	bash scripts/tests/test_apply_schema_terminology_path.sh
	bash scripts/tests/test_run_tests_path.sh
	bash scripts/tests/test_run_tests_no_embedded_workflow.sh
	bash scripts/tests/test_load_to_postgres_no_legacy_seed.sh
	bash scripts/tests/test_load_to_postgres_atomic.sh
	bash scripts/tests/test_patient_gender_index.sh
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
