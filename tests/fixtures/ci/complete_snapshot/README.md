# CI complete snapshot fixture

A small, deterministic, **entirely synthetic** CSV snapshot: exactly one
CSV per managed table in `tuva_postgres.manifest.MANAGED_TABLES` (15
files), each with a complete header (migration 0001's column set, in
ordinal order) and exactly one data row.

Contains no PHI, no real patient data, and no credentials. Every
identifier (`person-1`, `practitioner-1`, `location-1`, `encounter-1`,
...), date, and amount is fixed -- nothing here is generated from the
current date/time or from a random source. Every coded/terminology field
(diagnosis codes, NDC, HCPCS, etc.) is left blank/NULL on purpose, since
this fixture never loads the large terminology reference tables.

This fixture exists only to prove -- deterministically, in CI and
locally -- that migrations, the real `scripts/load_to_postgres.sh`
loader, and the full `db/tests/*.sql` validation suite work end to end.
It is not representative of real data volume or terminology coverage,
and it must never be pointed at by `DATA_DIR` for an actual data load.

See the repository's top-level `README.md` ("CI fixture and the
complete-run smoke test" section) for how to validate this fixture
without a database, how to run the full smoke test against a disposable
PostgreSQL database, and how to regenerate it safely when the baseline
schema changes.
