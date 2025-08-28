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
