"""PostgreSQL connection and low-level SQL helpers built on psycopg (v3).

`psycopg` is imported lazily inside functions (not at module scope) so
that other tuva_postgres modules -- and their unit tests -- can be
imported and exercised in an environment where the PostgreSQL driver
isn't installed (e.g. this repository's own CI/dev sandbox without
network access to build psycopg-binary). Anything that actually talks to
PostgreSQL will raise ImportError with a clear message if psycopg is
missing; nothing else does.

Identifier composition
-----------------------
`validated_identifier`/`qualified_relation` below are this repository's
low-level building blocks for turning a dynamic schema/relation name into
safe SQL *syntax* (as opposed to a bound *value* -- see
`tuva_postgres.identifiers` for why the distinction matters). Both
validate every component through the single shared policy in
`identifiers.py` before composing anything, and neither ever accepts a
pre-quoted string or a dotted "schema.table" pair as one argument --
callers always supply schema and relation separately so each is
validated on its own.

`qualified_relation` returns a plain Python string (`'"schema"."table"'`)
rather than a `psycopg.sql.Composed` object. This is a deliberate,
narrowly-scoped exception to "prefer psycopg.sql composition", made for
one specific reason: `ops.py` and `migrations.py` are called with a real
psycopg connection in production but with a lightweight fake connection
in their unit tests (see tests/unit/test_ops.py, tests/unit/
test_migrations.py) precisely so those tests can run without psycopg
installed at all. A real `psycopg.sql.Identifier` needs a live psycopg
import to construct; a validated, quoted plain string does not, so this
keeps that no-psycopg-required property intact. It is exactly as safe as
`psycopg.sql.Identifier` here because `identifiers.IDENTIFIER_PATTERN`
already rejects the one character (`"`) that would ever need escaping
inside a double-quoted identifier -- `quote_ident` still escapes it
defensively (never trust a single layer of validation alone), but a
value that reaches `quote_ident` has already been proven not to contain
one.

For call sites that already require a real psycopg connection and want
genuine `psycopg.sql` composables (e.g. building a `Composed` query to
pass straight to `cursor.execute()`), `identifier_sql`/
`qualified_identifier_sql` below do exactly that -- same validation,
`psycopg.sql.Identifier` output instead of a string. Both still validate
through the identical shared policy first.
"""
from __future__ import annotations

import re

from .identifiers import validate_identifier

_PSQL_STRING_VAR_RE_TEMPLATE = r":'{name}'"
_PSQL_IDENT_VAR_RE_TEMPLATE = r':"{name}"'


def _require_psycopg():
    try:
        import psycopg  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only where psycopg is absent
        raise ImportError(
            "psycopg is required for database operations but is not installed. "
            "Run `uv sync --locked` (see pyproject.toml)."
        ) from exc
    return psycopg


def connect(dsn: str):
    """Open a new psycopg connection. Callers own the connection's
    lifecycle (use as a context manager) and its transaction boundaries
    (autocommit is left at psycopg's default of off)."""
    psycopg = _require_psycopg()
    return psycopg.connect(dsn)


# --- Identifier validation + composition -----------------------------------


def validated_identifier(value: str, label: str) -> str:
    """Validate a single dynamic identifier component (schema name OR
    relation name -- never both combined) against the shared policy and
    return it unchanged. Thin, colocated re-export of
    `identifiers.validate_identifier` so every SQL-composition helper in
    this module lives next to the validation it depends on."""
    return validate_identifier(value, label)


def quote_ident(value: str) -> str:
    """Wrap an *already-validated* identifier in double quotes, doubling
    any embedded `"` as PostgreSQL requires for quoted identifiers.

    This is never a substitute for validation -- callers must validate
    `value` (e.g. via `validated_identifier`) before calling this. It
    exists as a narrowly-scoped, defense-in-depth quoting step: even
    though `identifiers.IDENTIFIER_PATTERN` already rejects `"` entirely
    (so a validated value can never actually contain one), this still
    escapes it explicitly rather than assuming the pattern can never
    change or be bypassed.
    """
    return '"' + value.replace('"', '""') + '"'


def qualified_relation(
    schema: str, relation: str, *, schema_label: str = "schema", relation_label: str = "relation"
) -> str:
    """Validate `schema` and `relation` independently against the shared
    identifier policy, then return a safely quoted, schema-qualified SQL
    identifier string: `'"schema"."relation"'`. Raises
    `identifiers.InvalidIdentifierError` (a `ValueError`) before composing
    anything if either component is invalid -- callers must call this
    (or `validated_identifier`) before touching a cursor, never after.

    Never accepts a single `"schema.relation"` string -- a dotted value
    would itself fail identifier validation (dots aren't in the allowed
    character set), which is exactly the point: schema and relation are
    always two independently validated components, never one opaque
    blob that might already contain SQL syntax.
    """
    schema = validated_identifier(schema, schema_label)
    relation = validated_identifier(relation, relation_label)
    return f"{quote_ident(schema)}.{quote_ident(relation)}"


def identifier_sql(value: str, label: str):
    """Validate `value` and return a real `psycopg.sql.Identifier` for
    callers that already hold a live psycopg connection and want a
    genuine `psycopg.sql` composable (e.g. to `.format()` into a
    `psycopg.sql.SQL(...)` template passed straight to
    `cursor.execute()`). Requires psycopg to be installed -- lazily
    imported here, not at module scope."""
    psycopg = _require_psycopg()
    validated = validated_identifier(value, label)
    return psycopg.sql.Identifier(validated)


def qualified_identifier_sql(
    schema: str, relation: str, *, schema_label: str = "schema", relation_label: str = "relation"
):
    """Validate `schema` and `relation` independently, then return a real
    `psycopg.sql.Identifier(schema, relation)` -- psycopg's own
    schema-qualified identifier composable. Requires psycopg to be
    installed. See `qualified_relation` for the equivalent that returns a
    plain string instead, for call sites that must stay usable without
    psycopg installed."""
    psycopg = _require_psycopg()
    schema = validated_identifier(schema, schema_label)
    relation = validated_identifier(relation, relation_label)
    return psycopg.sql.Identifier(schema, relation)


def substitute_psql_vars(sql_text: str, variables: dict[str, str]) -> str:
    """Replicate psql's `-v name=value` variable interpolation for the
    two forms this repository's SQL files use:

      :"name"   -> a double-quoted SQL identifier (embedded " doubled)
      :'name'   -> a single-quoted SQL string literal (embedded ' doubled)

    This lets migrations execute the version-owned DDL files under
    db/migrations/sql/ (written for psql's `-f` + `-v`) through a plain
    psycopg connection, without shelling out to the `psql` binary.

    Identifier-form (`:"name"`) substitutions are validated against the
    shared identifier policy before being composed into the rendered
    SQL, regardless of what the caller already validated -- this is the
    lowest-level point where psql-style substitution actually happens, so
    it validates again itself rather than trusting every caller to have
    done it correctly (defense in depth; see identifiers.py). Only
    variables actually used in `:"name"` form in `sql_text` are validated
    as identifiers -- a variable used only in `:'name'` (string-literal)
    form is never forced through identifier validation, since an
    ordinary string-literal placeholder is not an identifier and may
    legitimately contain characters an identifier cannot (this repository
    only ever binds schema names through either form, so in practice both
    forms of a given variable name are always identifier-safe values, but
    this function does not assume that of every possible caller).
    """
    result = sql_text
    for name, value in variables.items():
        ident_pattern = _PSQL_IDENT_VAR_RE_TEMPLATE.format(name=re.escape(name))
        if re.search(ident_pattern, result):
            validated = validate_identifier(value, name)
            ident = quote_ident(validated)
            result = re.sub(ident_pattern, lambda _m, v=ident: v, result)
        literal = "'" + value.replace("'", "''") + "'"
        result = re.sub(_PSQL_STRING_VAR_RE_TEMPLATE.format(name=re.escape(name)), lambda _m, v=literal: v, result)
    return result


def try_advisory_lock(conn, key: int) -> bool:
    """Session-level `pg_try_advisory_lock` -- non-blocking. Returns True
    if the lock was acquired. Must be released with `advisory_unlock`
    using the *same* connection/session."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
        (acquired,) = cur.fetchone()
    return bool(acquired)


def advisory_unlock(conn, key: int) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
    conn.commit()


# Fixed, documented advisory-lock key namespace (arbitrary but stable
# 63-bit integers, chosen once and never reused for another purpose).
MIGRATION_LOCK_KEY = 0x7A5A_4D49_4752  # "zZMIGR" ascii-ish, just needs to be stable
PIPELINE_LOCK_KEY = 0x7A5A_5049_5045  # "zZPIPE"
