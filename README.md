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
(not-applied-by-this-repo) Kubernetes deployment.

New Makefile targets: `deps`, `fetch`, `migrate`, `migration-status`,
`pipeline`, `health`, `test-unit`, `test-integration` (requires a
disposable `PG_DSN`), `test-container`, `test-deploy`, `docker-build`,
`compose-up`, `compose-down`, and the `local-db-*`/`test-compose-integration`
targets below.

## Local PostgreSQL with Docker Compose

A disposable, one-command local PostgreSQL database for development --
this is a separate concern from three other things this repository also
has, and it's worth being clear about which is which:

- **Local PostgreSQL (this section, `compose.yaml`)** -- a throwaway
  database on your machine for day-to-day development. Local-only
  example credentials, no TLS, no backups, no HA.
- **The application pipeline container (`Dockerfile`, the `pipeline`
  service in `compose.yaml`)** -- the same production image, runnable
  locally against that disposable database for `healthcheck`/`migrate`,
  or a full `run` if you also supply `TUVA_API_MANIFEST_URL`/
  `TUVA_API_TOKEN`.
- **Production Kubernetes deployment (`deploy/kubernetes/`)** -- what
  actually runs in production; see `docs/RUNBOOK.md`. Nothing in
  `compose.yaml` is applied to, or used by, production -- Compose is a
  local development convenience only.

### Prerequisites

- Docker Engine or Docker Desktop
- Docker Compose v2 (the `docker compose` subcommand, not the standalone
  `docker-compose` v1 binary)

### One-command startup

```bash
make local-db-ready
```

Starts Postgres (detached), waits for it to report healthy, then applies
every migration through the repository's real migration runner (`tuva-
postgres migrate` -- the exact same code path as `make migrate` against
any other database; nothing is duplicated into a Postgres init script).
Safe to rerun any time; it never requires `TUVA_API_MANIFEST_URL`/
`TUVA_API_TOKEN`, and never deletes existing data.

### Host connection information

The command above prints the host DSN when it finishes. It matches
`compose.yaml`'s local-only example credentials exactly:

```
postgresql://tuva_local:local-only-example-password-change-me@127.0.0.1:5432/tuva
```

**The password above is intentionally not a secret.** It is a clearly
labeled, local-development-only placeholder baked into `compose.yaml`
for convenience -- never reuse it anywhere outside your own machine, and
never treat it as if it protects anything.

Load host-side environment variables (schemas included) into your shell:

```bash
cp scripts/setup_local_postgres.example .env   # or scripts/setup_env.example
. .env
```

Both example files already match `compose.yaml`'s credentials, database
name, and schemas (`tuva` / `tuva_term` / `tuva_ops`) -- see
`scripts/setup_local_postgres.example` for the full explanation of how
Compose's own `${VAR:-default}` interpolation and shell-sourcing a `.env`
file are two different mechanisms that happen to use similar syntax.

### Everyday commands

```bash
make local-db-status   # container state + migration status (read-only)
make local-db-migrate  # (re)apply migrations -- safe to rerun, exits nonzero on failure
make local-db-shell    # psql against the local database (no host psql required)
make local-db-logs     # follow Postgres logs (Ctrl-C to stop following)
```

### Stopping and restarting (data preserved)

```bash
make local-db-down     # stops containers; the Postgres data volume is preserved
make local-db-ready     # starts again -- your data is exactly as you left it
```

Routine shutdown (`make local-db-down`, and `make compose-down`) never
passes `-v` to `docker compose down` -- local database data survives an
ordinary stop/start cycle.

### Resetting all local data (destructive)

```bash
make local-db-reset
```

This **permanently deletes** the local Postgres data volume (and this
stack's other local-only volumes). It requires either an interactive
`yes` confirmation, or `CONFIRM_LOCAL_DB_RESET=yes` for scripted/
noninteractive use:

```bash
CONFIRM_LOCAL_DB_RESET=yes make local-db-reset
```

It only ever touches this Compose project's own resources -- never
`docker system prune`, never another project's containers or volumes.

### Changing the host port

If port 5432 is already in use on your machine, set `POSTGRES_PORT`
before starting the stack:

```bash
POSTGRES_PORT=5433 make local-db-ready
```

The container-side port always stays Postgres's normal 5432; only the
host-side publish port changes. Update `PG_DSN`/`.env` accordingly if you
load one (`scripts/setup_local_postgres.example` already references
`${POSTGRES_PORT:-5432}`).

### Running the Compose integration smoke test

```bash
make test-compose-integration
```

Spins up an **isolated**, uniquely-named Compose project (never your own
`local-db-*` stack) on a dynamically chosen port, proves the whole
workflow end to end against a real Postgres (config rendering, health,
migrations, expected schemas, idempotent reapply, stop/start data
persistence), and cleans up fully on exit. Requires Docker; prints a
clear `SKIPPED` message and exits successfully if Docker isn't available.
The database-free structural counterpart (`tests/unit/
test_local_postgres_compose.py`, run via `make test-unit`) always runs,
with or without Docker.

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

Every manifest also declares exactly one **execution mode** via a
required `"execution"` field:

- `"one_time"` -- applied at most once. Once applied, its SQL, file
  order, checksum, and execution mode are all immutable; any drift is a
  hard error that blocks all further migration activity. Migrations
  `0001` and `0002` are both `one_time`.
- `"repeatable"` -- applied on first discovery, then transactionally
  reapplied whenever its checksum changes, and skipped otherwise
  (standard checksum-driven semantics -- never rerun unconditionally).
  Use this for idempotent SQL (`CREATE OR REPLACE VIEW`/function, etc.)
  you want to keep current, not a one-off schema change.

`src/tuva_postgres/migrations.py` computes each migration's checksum
purely from its ordered files' basenames, byte lengths, and contents
(manifest metadata like `execution` never affects it), and refuses to
proceed if an already-applied `one_time` migration's checksum -- or any
migration's execution mode -- has changed since it was applied. Within a
single run, all pending `one_time` migrations apply (ascending version)
before any pending `repeatable` migration, so a repeatable view or
function can safely depend on a schema object a pending `one_time`
migration is about to create, regardless of version numbering. Database
changes always go into a **new** migration at the next unused numeric
version (`0003`, `0004`, ...) -- see `docs/RUNBOOK.md`'s "Adding a new
migration" section for the full walkthrough, including execution-mode
guidance.

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

### Migration idempotency

CI runs the real migration runner (`scripts/apply_schema.sh`) **twice**
against the same unique, disposable schemas, as a dedicated step ("Run
migration suite twice and verify idempotency") that runs before schema
creation/fixture loading/SQL validation on every push, pull request, and
scheduled run. The first invocation applies every pending migration; the
second, identical invocation must be a true no-op -- it never bypasses
`schema_migrations` to hand-rerun `one_time` SQL, and it must report zero
migrations applied. The check compares, byte-for-byte, before and after
the second run: the complete migration-history table (every column,
including `execution_count`), a deterministic catalog fingerprint
(schemas, tables, views, columns, constraints, indexes, functions, and
non-internal triggers), and the existing focused foreign-key catalog. It
also confirms the final `--status` output has zero pending migrations and
no checksum/execution-mode mismatches, and that `--status` itself never
mutates history. This protects `one_time` immutability and unchanged-
`repeatable` reapplication-skipping -- it is a separate concern from
testing a *changed* repeatable migration's re-execution (see
`scripts/tests/test_migration_execution_modes.sh`).

Run it locally with `make test-schema-idempotency` (requires a real,
**disposable** PostgreSQL database via `PG_DSN` in `.env` -- never point
it at production; it creates and drops its own uniquely-named temporary
schemas and never touches `tuva`/`tuva_term`/`tuva_ops`). It's
intentionally excluded from `test-shell`/`test` since it needs Postgres.
The harness's control flow (does it correctly accept a true no-op rerun
and correctly reject every way a rerun can go wrong?) has separate,
database-free regression coverage in
`scripts/tests/test_schema_idempotency_harness_controls.sh`, run as part
of `make test-shell`.

## Python development and quality tooling

`src/tuva_postgres/` has two runtime dependencies -- `requests` (the API
client) and `psycopg[binary]` (migrations, the orchestrator's database
access) -- both exact-pinned in `pyproject.toml`. The plain shell scripts
under `scripts/` still use only the Python standard library.

Everything else is a single, locked dev/tooling toolchain, also
exact-pinned in `pyproject.toml` (`[dependency-groups] dev`) and locked
(with every transitive dependency) in the committed `uv.lock`, so local
and CI runs always resolve the identical versions -- never an unpinned
`pip install`:
- **Ruff** -- lint (`ruff check`) and format (`ruff format`) for
  `src/`, `tests/`, and `scripts/`. See `[tool.ruff]` /
  `[tool.ruff.lint]` in `pyproject.toml` for the configured rule set
  (`E4`, `E7`, `E9`, `F`, `I`, `UP`, `B`) and the narrow, justified
  per-file ignores.
- **mypy** -- static typing, scoped to the production package only
  (`files = ["src/tuva_postgres"]` in `[tool.mypy]`). A strong-but-honest
  configuration, not full strict mode: `check_untyped_defs`,
  `no_implicit_optional`, `warn_redundant_casts`, `warn_unused_ignores`,
  `warn_unreachable`, and `strict_equality` are all on, but there's no
  blanket `ignore_errors`/`ignore_missing_imports` and no
  `disallow_untyped_defs`.
- **pytest** -- the test runner for both `tests/unit/` (database-free)
  and `tests/integration/` (requires a disposable PostgreSQL database).
  Both suites are `unittest.TestCase`-based; pytest collects and runs
  them natively, no rewrite needed. See `[tool.pytest.ini_options]` in
  `pyproject.toml` -- the `integration` marker is what keeps a plain
  `pytest` run database-free by default.
- **SQLFluff** -- SQL lint/format via the psql-aware wrapper
  (`scripts/sqlfluff_psql_wrapper.sh`), which normalizes psql identifier
  variables (`:"schema"`, `:"terminology_schema"`, `:"ops_schema"`) before
  linting so checksum-protected migration SQL under `db/migrations/` can
  be linted without ever modifying its committed content. **Migration
  SQL is never rewritten by any tool** -- the SQLFluff fix hook only
  previews suggested changes to stdout; see "Database migrations" above
  for the checksum contract this protects.
- **pre-commit** -- orchestrates all of the above (plus a few general
  hygiene hooks) as local, `uv`-managed git hooks.

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

Day-to-day commands:
```bash
make test-unit             # uv run pytest tests/unit (database-free)
make test-integration       # uv run pytest tests/integration (requires PG_DSN)
make lint-python             # uv run ruff check src tests scripts
make format-python           # uv run ruff format src tests scripts (rewrites in place)
make format-python-check     # uv run ruff format --check src tests scripts (CI-safe, no rewrite)
make typecheck                # uv run mypy src/tuva_postgres
make lint-sql                 # SQLFluff lint via the psql-aware wrapper, every tracked *.sql file
make quality                  # check-python-deps + lint-python + format-python-check + typecheck + test-unit + lint-sql
make lint                     # uv run pre-commit run --all-files (every hook, local uv-managed environment)
make fmt                      # ruff format (rewrites) + the manual-only SQLFluff fixer (preview only, see above)
make check-python-deps        # verify the locked toolchain is current and installable (no database required)
```

`make quality` is the single database-free gate to run before every push
(and is what CI runs, via the same `make` targets -- see
`.github/workflows/ci.yml`). Every local pre-commit hook's `entry:` is
prefixed `uv run` directly, so hooks resolve the locked `.venv` correctly
whether triggered via `make lint`, the installed git hook, or CI.

**Updating a pinned dependency (Ruff, mypy, pytest, SQLFluff, pre-commit,
or a transitive package) intentionally:**
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
