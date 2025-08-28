.PHONY: init create-db load test lint fmt

init:
	python3 -m venv .venv && . .venv/bin/activate && pip install -U pip pre-commit
	pre-commit install
	@echo "Copying env template -> .env (edit it!)"
	cp -n scripts/setup_env.example .env || true

create-db:
	. .env && psql "$$PG_DSN" -v ON_ERROR_STOP=1 -f db/schema.sql

load:
	. .env && bash scripts/load_to_postgres.sh

test:
	. .env && psql "$$PG_DSN" -v ON_ERROR_STOP=1 -f db/tests.sql

lint:
	pre-commit run --all-files

fmt:
	@echo "Add formatters here if you later include sqlfluff/ruff, etc."
