"""The single, authoritative validation policy for dynamic PostgreSQL
identifiers (schema and relation names) used anywhere in this repository.

Every other module that needs to accept a schema/relation name from
configuration, a manifest, or a caller -- `config.py`, `db.py`,
`ops.py`, `migrations.py`, and the standalone `scripts/*.py` utilities
that talk to PostgreSQL -- imports `validate_identifier` from here rather
than keeping its own copy of the regex. One policy, enforced in one
place, is much easier to audit than the same pattern re-typed (and
potentially re-typed *wrong*) in half a dozen files.

Why identifiers get a dedicated validator instead of just using SQL
parameters: PostgreSQL's wire protocol (and psycopg's `%s` parameter
binding) can only bind *values* -- a schema or table name is a piece of
SQL syntax, not a value, so it can never be a bind parameter. A schema
name has to be validated and safely composed into the SQL text itself
(quoted as an identifier) before the statement is even prepared. Mixing
these up -- passing an identifier through value binding, or (the
dangerous direction) splicing a value into SQL text as if it were an
identifier -- is exactly the class of bug this module exists to prevent
downstream of.

This module is deliberately dependency-light: no `import psycopg`, no
project-internal imports of `config`, `migrations`, `db`, or
`orchestrator` (to avoid any risk of a circular import, and so this
module can be imported -- and its policy unit-tested -- in an
environment with nothing but the Python standard library installed, long
before `uv sync` has ever run).
"""
from __future__ import annotations

import re

__all__ = ["InvalidIdentifierError", "IDENTIFIER_PATTERN", "validate_identifier", "validate_identifiers"]

# ASCII letters/digits/underscores only, must start with a letter or
# underscore. No dots (never accept "schema.table" as a single value --
# callers must supply schema and relation as separate, independently
# validated components), no quotes, no whitespace, no semicolons, no SQL
# comment markers, no null bytes, no Unicode lookalikes -- anything
# outside `[A-Za-z0-9_]` is rejected by construction, not by an explicit
# denylist. `fullmatch()` (never `match()` + a trailing `$`) is used
# throughout so a value that is otherwise valid except for a trailing
# newline is correctly rejected: Python's `$` anchor matches just before
# a trailing "\n", so `PATTERN.match(value)` on "tuva\n" would (wrongly)
# succeed, while `PATTERN.fullmatch(value)` correctly requires every
# character -- including that trailing newline -- to be consumed by the
# pattern, and rejects it.
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class InvalidIdentifierError(ValueError):
    """Raised by `validate_identifier`/`validate_identifiers` for any
    value that fails the identifier policy: wrong type, empty, or
    containing any character outside `[A-Za-z0-9_]` (including dots,
    quotes, whitespace, semicolons, comment markers, null bytes, or
    non-ASCII text).

    A `ValueError` subclass so existing `except ValueError` handling
    keeps working; callers that need a domain-specific error translate
    this into one (see `config.PipelineConfig.load` -> `ConfigError`,
    `migrations._resolve_vars` -> `MigrationError`).

    The message never includes anything beyond the offending label and
    value itself -- there is nothing secret about a schema name, but this
    error is also never used for values that might carry one (a DSN, an
    API token, ...); those have their own sanitized error paths.
    """

    def __init__(self, label: str, value: object, *, reason: str | None = None) -> None:
        self.label = label
        self.value = value
        detail = reason or "is not a safe SQL identifier"
        message = (
            f"{label}={value!r} {detail} (expected: ASCII letters/digits/underscores only, "
            "starting with a letter or underscore -- no dots, quotes, whitespace, semicolons, "
            "comments, or other special characters)"
        )
        super().__init__(message)


def validate_identifier(value: object, label: str) -> str:
    """Validate `value` as a single PostgreSQL identifier component
    (a schema name, or a relation name -- never a dotted "schema.table"
    pair; see the module docstring). Returns `value` completely unchanged
    when valid -- this function never normalizes, trims, lowercases,
    truncates, or otherwise rewrites the input, so the exact string a
    caller passed is exactly the string that reaches SQL composition.

    Raises `InvalidIdentifierError` (a `ValueError`) for anything else,
    including a non-string `value`. `label` is included in the error only
    to make the failure actionable (e.g. "PG_SCHEMA" or "OPS_SCHEMA") --
    it is never validated itself.
    """
    if not isinstance(value, str):
        raise InvalidIdentifierError(label, value, reason=f"must be a string, got {type(value).__name__}")
    if not value:
        raise InvalidIdentifierError(label, value, reason="must not be empty")
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise InvalidIdentifierError(label, value)
    return value


def validate_identifiers(*pairs: tuple[object, str]) -> tuple[str, ...]:
    """Validate several `(value, label)` pairs at once, in order, each
    against the same policy as `validate_identifier`. Returns a tuple of
    the validated values (unchanged, same order) on success. Raises on
    the *first* invalid pair -- this is a low-level, execution-boundary
    guard, not a user-facing form validator that needs to collect every
    problem at once (see `config.PipelineConfig.load` for that; it calls
    `validate_identifier` per-field so it can keep accumulating errors)."""
    return tuple(validate_identifier(value, label) for value, label in pairs)
