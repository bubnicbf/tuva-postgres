.PHONY: init create-db load test test-shell lint fmt

init:
	python3 -m venv .venv && . .venv/bin/activate && pip install -U pip pre-commit
	pre-commit install
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
	bash scripts/tests/test_patient_gender_index.sh

test: test-shell
	. .env && bash scripts/run_tests.sh

lint:
	pre-commit run --all-files

fmt:
	@echo "Add formatters here if you later include sqlfluff/ruff, etc."
