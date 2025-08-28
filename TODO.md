TODO:
- CI: GitHub Actions workflow to run make create-db load test in a service container (Postgres 16)
- Data diffs: add a scripts/diff_counts.sql to compare row counts across runs/releases
- Column mapping catalog: a docs/mapping.md that pins Tuva CSV column → table.column mapping for maintainability
- add a compact summary view that collects all test results into a single table (for CI parsing), or wire a GitHub Actions job that brings up a Postgres service and runs make create-db load test
- add the same NPI validator as a check on encounter.attending_provider_id → practitioner.npi join (for data hygiene)
- add a matrix to test Postgres 14/15/16 to CI YAML
- add a results view specific to medical_claim (e.g., quick per-payer/plan failure counts) for rapid triage
