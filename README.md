# tuva-postgres
Reproducible Postgres load of Tuva seed datasets.

## Quickstart
```bash
make init
cp scripts/setup_env.example .env  # edit DSN / schema
make create-db
python scripts/normalize_csvs.py data
make load
make test
```

## Notes

- Put CSVs in data/ with headers matching db/schema.sql.
- Adjust table/column names to the Tuva release you use.
- scripts/load_to_postgres.sh uses \copy, so no server-side file access needed.

### Loading is an atomic snapshot replacement

`make load` (`scripts/load_to_postgres.sh`) treats the CSVs in `DATA_DIR` as a
complete, replaceable snapshot, not an append-only stream:

- All managed tables are truncated together and every CSV is copied in
  within a single PostgreSQL transaction, committed only if every copy
  succeeds. A failure partway through rolls back the whole transaction, so
  the previous snapshot is left untouched.
- Re-running the same (or a corrected) snapshot is safe: existing rows are
  replaced, not appended, so retries never raise duplicate-key errors.
- A complete set of CSVs is required. If some but not all managed tables'
  CSVs are present, the loader refuses to run rather than load a partial
  dataset. If none are present, it's a no-op.

Run `make test-load-integration` (requires a real, disposable `PG_DSN`) to
verify this against a live database: it loads a snapshot twice to confirm
retries don't duplicate rows, then loads an intentionally invalid snapshot
to confirm the prior snapshot survives a failed load intact.

---

# Git initialization & message style

**Use Conventional Commits** so your history remains parseable and clean.

- `feat`: new capability (tables, loader features)
- `fix`: bug fixes (schema mismatch, data type correction)
- `docs`: README, notes
- `chore`: non-prod changes (gitignore, boilerplate)
- `refactor`: non-bug, non-feature structural changes
- `test`: tests only
- `ci`/`build`: pipeline & deps

**One-time setup**
```bash
git init
git config commit.template .commit-template.txt
git add .
git commit -m "chore(repo): bootstrap Postgres Tuva loader scaffold"
