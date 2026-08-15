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

## Production ingestion pipeline

Beyond the plain CSV loader above, `src/tuva_postgres/` is a full
production pipeline: an authenticated API client speaking a versioned
JSON manifest contract, an immutable raw landing layer, tracked database
migrations, an observable orchestrator (`fetch -> migrate -> load ->
test`), a production container, and a scheduled Kubernetes `CronJob`.

```bash
uv sync --locked                        # installs requests + psycopg (see below)
cp scripts/setup_env.example .env       # fill in TUVA_API_MANIFEST_URL/TOKEN, PG_DSN, etc.
make migrate                            # or: uv run tuva-postgres migrate
make pipeline                           # or: uv run tuva-postgres run
make health                             # or: uv run tuva-postgres healthcheck
```

See **`docs/RUNBOOK.md`** for the full operations guide (required
config, scheduled runs, reading structured logs, querying run/artifact
history, handling checksum/migration failures, retention, and
recommended alerts), **`docs/API_MANIFEST.md`** for the manifest contract
the API client speaks, and **`deploy/kubernetes/README.md`** for the
(not-applied-by-this-repo) Kubernetes deployment. Local container
development: `docker compose up --build` (see `compose.yaml`).

New Makefile targets: `deps`, `fetch`, `migrate`, `migration-status`,
`pipeline`, `health`, `test-unit`, `test-integration` (requires a
disposable `PG_DSN`), `test-container`, `test-deploy`, `docker-build`,
`compose-up`, `compose-down`.

## Database migrations

`db/migrations/` is the **sole authoritative home for deployable DDL** --
there is no other place a table, view, or constraint definition lives.
Each migration is a versioned JSON manifest (`db/migrations/{version}_
{slug}.json`) plus one or more SQL files it owns exclusively, under
`db/migrations/sql/{version}_{slug}/` (see `db/migrations/0001_baseline.json`
/ `db/migrations/sql/0001_baseline/{core,views,terminology}/` and
`db/migrations/0002_operational_schema.json` /
`db/migrations/sql/0002_operational_schema/`). The manifest's `files` list
is the authoritative execution order -- never filesystem traversal order.

Applied migrations are immutable: never edit an existing migration's
files or reorder its manifest once it has shipped. `src/tuva_postgres/
migrations.py` computes each migration's checksum from its ordered
files' basenames, byte lengths, and contents, and refuses to proceed if
an already-applied migration's checksum has changed. Database changes
always go into a **new** migration at the next unused numeric version
(`0003`, `0004`, ...) -- see `docs/RUNBOOK.md`'s "Adding a new migration"
section for the full walkthrough.

SQL data-quality/validation queries (the smoke tests and add-on checks
`scripts/run_tests.sh` runs after a load) are a separate concern and live
under `db/tests/`, not `db/migrations/` -- they are never treated as
deployable DDL, and new SQL validation tests should be added there (new
deployable DDL, by contrast, always goes into a new migration -- see
above). `db/tests/zz_results.sql` initializes the `test_results` table
and summary views and is applied once as setup, not as a validation case;
every other `db/tests/*.sql` file is a validation case, executed in
deterministic filename order by `scripts/run_tests.sh` -- the
authoritative SQL-test runner, invoked via `make test` (requires the
configured database) or directly as `uv run tuva-postgres test`.
`make test-shell` is the database-free counterpart: it validates
migration and SQL-test-runner *structure and behavior* (via stubbed
`psql`/`python3`) without needing a real Postgres connection.

`make create-db` / `make migrate` apply pending migrations transactionally
(see `scripts/apply_schema.sh` -> `tuva_postgres.migrations`);
`make migration-status` reports applied, pending, and checksum-mismatch
states without applying anything.

## Python tooling (SQLFluff, pre-commit, requests, psycopg)

`src/tuva_postgres/` has two runtime dependencies -- `requests` (the API
client) and `psycopg[binary]` (migrations, the orchestrator's database
access) -- both exact-pinned in `pyproject.toml`. The plain shell scripts
under `scripts/` still use only the Python standard library. SQLFluff and
pre-commit are dev/tooling-only dependencies. All of the above are
declared with exact pins in `pyproject.toml` and locked (with every
transitive dependency) in the committed `uv.lock`, so local and CI runs
always resolve the identical versions.

Prerequisites:
- Python 3.12 (selected in `.python-version`; `requires-python = ">=3.12"`
  in `pyproject.toml`)
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/)

Setup:
```bash
make init          # uv sync --locked && uv run pre-commit install
```
or, equivalently, without also installing the git hook:
```bash
uv sync --locked
```

Both create/update `.venv` from `uv.lock` exactly -- never an unpinned
`pip install sqlfluff` / `pip install pre-commit`.

Running lint (all `.pre-commit-config.yaml` hooks, via the locked
environment):
```bash
make lint           # uv run pre-commit run --all-files
```

Running the manual-only SQL formatter:
```bash
make fmt             # uv run pre-commit run --hook-stage manual sqlfluff-psql-fix --all-files
```

Verifying the locked toolchain is current and installable (no database
required):
```bash
make check-python-deps
```

**Updating a pinned dependency (SQLFluff, pre-commit, or a transitive
package) intentionally:**
1. Edit the direct pin(s) in `pyproject.toml` (`[dependency-groups] dev`).
2. Regenerate the lockfile: `uv lock`.
3. Validate the result installs cleanly: `uv sync --locked`.
4. Commit `pyproject.toml` and `uv.lock` together in the same commit.

## Notes

- Put CSVs in data/ with headers matching the applicable table definitions
  in the baseline migration DDL under db/migrations/sql/0001_baseline/core/
  and db/migrations/sql/0001_baseline/terminology/ (see
  db/migrations/0001_baseline.json for the full, ordered list -- see
  "Database migrations" below for why db/migrations/ is the only place to
  look).
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
