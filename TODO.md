TODO:
- SQL linting with sqlfluff + a pyproject.toml config, and a pre-commit hook
- CI: GitHub Actions workflow to run make create-db load test in a service container (Postgres 16)
- Data diffs: add a scripts/diff_counts.sql to compare row counts across runs/releases
- Column mapping catalog: a docs/mapping.md that pins Tuva CSV column → table.column mapping for maintainability
- add a compact summary view that collects all test results into a single table (for CI parsing), or wire a GitHub Actions job that brings up a Postgres service and runs make create-db load test
