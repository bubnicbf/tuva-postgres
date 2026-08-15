"""PostgreSQL connection and low-level SQL helpers built on psycopg (v3).

`psycopg` is imported lazily inside functions (not at module scope) so
that other tuva_postgres modules -- and their unit tests -- can be
imported and exercised in an environment where the PostgreSQL driver
isn't installed (e.g. this repository's own CI/dev sandbox without
network access to build psycopg-binary). Anything that actually talks to
PostgreSQL will raise ImportError with a clear message if psycopg is
missing; nothing else does.
"""
from __future__ import annotations

import re

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


def substitute_psql_vars(sql_text: str, variables: dict[str, str]) -> str:
    """Replicate psql's `-v name=value` variable interpolation for the
    two forms this repository's SQL files use:

      :"name"   -> a double-quoted SQL identifier (embedded " doubled)
      :'name'   -> a single-quoted SQL string literal (embedded ' doubled)

    This lets migrations execute the existing db/tables/*.sql files
    (written for psql's `-f` + `-v`) through a plain psycopg connection,
    without shelling out to the `psql` binary.
    """
    result = sql_text
    for name, value in variables.items():
        ident = '"' + value.replace('"', '""') + '"'
        result = re.sub(_PSQL_IDENT_VAR_RE_TEMPLATE.format(name=re.escape(name)), lambda _m, v=ident: v, result)
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
