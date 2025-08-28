TODO:
- SQL linting with sqlfluff + a pyproject.toml config, and a pre-commit hook.
- CI: GitHub Actions workflow to run make create-db load test in a service container (Postgres 16).
- Data diffs: add a scripts/diff_counts.sql to compare row counts across runs/releases.
- Column mapping catalog: a docs/mapping.md that pins Tuva CSV column → table.column mapping for maintainability.