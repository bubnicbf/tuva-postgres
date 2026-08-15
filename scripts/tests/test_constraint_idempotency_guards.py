#!/usr/bin/env python3
"""Structural regression test for db/tables/*.sql constraint idempotency.

PostgreSQL has no `ADD CONSTRAINT IF NOT EXISTS`. Several core table files
under db/tables/ used to run unconditional statements like:

    ALTER TABLE :"schema".appointment
      ADD CONSTRAINT appt_person_fk
      FOREIGN KEY (person_id) REFERENCES :"schema".patient(person_id)
      DEFERRABLE INITIALLY DEFERRED;

which fail with a duplicate-constraint error on a second deployment. The
fix wraps each such statement in a `DO $$ ... END$$;` block that checks
pg_constraint/pg_class/pg_namespace (scoped by schema, owning table, and
constraint name) before executing the ALTER TABLE via EXECUTE format(...).

This test scans the SQL source as text -- it is not a general SQL parser,
just enough of one for this repository's style -- and verifies:

  * every active `ALTER TABLE ... ADD CONSTRAINT ...` statement is
    wrapped in an existence-check guard (not run unconditionally at the
    top level of the file);
  * foreign-key guards are scoped by schema, owning table, AND
    constraint name (a table can have more than one FK, so constraint
    name matters for those);
  * non-FK guards (e.g. the pre-existing terminology PRIMARY KEY guards)
    are at least scoped by schema and owning table -- these predate this
    fix and are intentionally left as-is, since a table has at most one
    primary key so table+schema alone is unambiguous;
  * nobody has "fixed" this by inventing
    `ADD CONSTRAINT IF NOT EXISTS`, which PostgreSQL does not support;
  * the complete, known inventory of core foreign-key constraints this
    task fixes is actually discovered, so the test cannot pass simply
    because its parser matched nothing.

Standard library only -- no PostgreSQL, network, or third-party
dependencies required.

Usage:
    python3 scripts/tests/test_constraint_idempotency_guards.py [repo_root]

With no argument, scans the real repository (located relative to this
script's own path). An optional repo_root argument lets a negative
control point this test at a scratch fixture instead, without ever
touching the real, committed SQL files.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Expected inventory (see task description / commit that introduced this
# test). If the parser can't find at least these, something is wrong with
# either the parser or the fix -- the test must not pass vacuously.
# ---------------------------------------------------------------------------
EXPECTED_FK_CONSTRAINTS = {
    ("appointment.sql", "appt_person_fk"),
    ("appointment.sql", "appt_encounter_fk"),
    ("appointment.sql", "appt_practitioner_fk"),
    ("condition.sql", "condition_person_fk"),
    ("condition.sql", "condition_encounter_fk"),
    ("eligibility.sql", "elig_person_fk"),
    ("encounter.sql", "encounter_person_fk"),
    ("encounter.sql", "encounter_attending_pr_fk"),
    ("immunization.sql", "imm_person_fk"),
    ("immunization.sql", "imm_encounter_fk"),
    ("immunization.sql", "imm_practitioner_fk"),
    ("lab_result.sql", "lab_result_person_fk"),
    ("lab_result.sql", "lab_result_encounter_fk"),
    ("medical_claim.sql", "mc_person_fk"),
    ("medical_claim.sql", "mc_encounter_fk"),
    ("medication.sql", "med_person_fk"),
    ("medication.sql", "med_encounter_fk"),
    ("medication.sql", "med_practitioner_fk"),
    ("observation.sql", "observation_person_fk"),
    ("observation.sql", "observation_encounter_fk"),
    ("person_id_crosswalk.sql", "pxw_person_fk"),
    ("pharmacy_claim.sql", "rx_person_fk"),
    ("procedure.sql", "procedure_person_fk"),
    ("procedure.sql", "procedure_encounter_fk"),
    ("procedure.sql", "procedure_practitioner_fk"),
}

INVALID_SYNTAX_RE = re.compile(r"ADD\s+CONSTRAINT\s+IF\s+NOT\s+EXISTS", re.IGNORECASE)

# Matches "ALTER TABLE <target> ADD CONSTRAINT <name> ..." wherever it
# appears in the comment-stripped text (either as a bare top-level
# statement, or embedded inside a string literal passed to EXECUTE
# format(...)).
ALTER_ADD_CONSTRAINT_RE = re.compile(
    r"ALTER\s+TABLE\s+(?P<target>\S+)\s+ADD\s+CONSTRAINT\s+(?P<name>\w+)",
    re.IGNORECASE,
)

FOREIGN_KEY_RE = re.compile(r"FOREIGN\s+KEY", re.IGNORECASE)

# DO $$ ... END$$; and DO $tag$ ... END $tag$; (any dollar-quote tag,
# matched via backreference so the opener and closer agree).
DO_BLOCK_RE = re.compile(r"DO\s+(\$[A-Za-z_]*\$).*?END\s*\1\s*;", re.DOTALL | re.IGNORECASE)

# A conditional existence guard: either the idiomatic "IF NOT EXISTS ("
# used directly in the IF condition, or a precomputed boolean flag pattern
# like "IF NOT pk_on_cvx THEN" (still driven by a pg_constraint lookup,
# just assigned to a variable first). Either is an approved guard shape.
CONDITIONAL_GUARD_RE = re.compile(r"\bIF\s+NOT\s+\w", re.IGNORECASE)

# A catalog existence check scoped by schema: "n.nspname = :'schema_var'"
# (a psql variable) or "n.nspname = current_schema()" (relies on the
# wrapper having already SET search_path to the target schema -- an
# equally valid, already-used alternative in this repo's terminology files).
SCHEMA_SCOPE_RE = re.compile(
    r"nspname\s*=\s*(:'[A-Za-z_][A-Za-z0-9_]*'|current_schema\(\s*\))",
    re.IGNORECASE,
)


def strip_comments(text: str) -> str:
    """Remove SQL line comments ("-- ... " to end of line), but only when
    not inside a single-quoted string or a dollar-quoted block ($$...$$ /
    $tag$...$tag$). Preserves line breaks and all non-comment content so
    downstream regexes and (if ever needed) line numbers stay meaningful.
    """
    out = []
    i = 0
    n = len(text)
    in_single = False
    dollar_tag: str | None = None  # e.g. "$$" or "$foo$" when inside one

    while i < n:
        ch = text[i]

        # Inside a dollar-quoted block: look only for the matching closer.
        if dollar_tag is not None:
            if text.startswith(dollar_tag, i):
                out.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
            else:
                out.append(ch)
                i += 1
            continue

        # Inside a single-quoted string: '' is an escaped quote.
        if in_single:
            if ch == "'":
                if text[i : i + 2] == "''":
                    out.append("''")
                    i += 2
                    continue
                in_single = False
                out.append(ch)
                i += 1
            else:
                out.append(ch)
                i += 1
            continue

        # Not inside any string: check for comment / quote / dollar-quote start.
        if text[i : i + 2] == "--":
            j = text.find("\n", i)
            if j == -1:
                break  # rest of file is a comment
            i = j  # leave the newline itself for the caller
            continue

        if ch == "'":
            in_single = True
            out.append(ch)
            i += 1
            continue

        if ch == "$":
            m = re.match(r"\$[A-Za-z_]*\$", text[i:])
            if m:
                dollar_tag = m.group(0)
                out.append(dollar_tag)
                i += len(dollar_tag)
                continue

        out.append(ch)
        i += 1

    return "".join(out)


def find_do_blocks(cleaned_text: str) -> list[tuple[int, int, str]]:
    """Return (start, end, block_text) for every DO $$ ... END$$; block."""
    return [(m.start(), m.end(), m.group(0)) for m in DO_BLOCK_RE.finditer(cleaned_text)]


def analyze_file(path: Path) -> tuple[list[str], list[tuple[str, str]]]:
    """Scan one SQL file.

    Returns (failures, guarded_constraints) where guarded_constraints is a
    list of (constraint_name, kind) for every constraint this file
    correctly guards ("fk" or "other").
    """
    raw = path.read_text()
    failures: list[str] = []

    if INVALID_SYNTAX_RE.search(raw):
        failures.append(
            f"{path.name}: uses invalid 'ADD CONSTRAINT IF NOT EXISTS' syntax "
            "(PostgreSQL does not support this)"
        )

    cleaned = strip_comments(raw)
    do_blocks = find_do_blocks(cleaned)

    guarded: list[tuple[str, str]] = []

    for m in ALTER_ADD_CONSTRAINT_RE.finditer(cleaned):
        target = m.group("target")
        conname = m.group("name")
        pos = m.start()

        # Table name without schema-variable prefix, e.g. ':"schema".appointment'
        # -> "appointment", or (inside a format string) '%I.appointment' -> "appointment",
        # or a bare unqualified name relying on search_path -> unchanged.
        table_name = re.sub(r"^.*[.\"]", "", target).strip("'\";")

        # The dispositive question is not *how* the ALTER TABLE text is
        # written (as a literal statement or as a string literal passed to
        # EXECUTE format(...)) -- both are valid guard shapes used in this
        # repo -- but *whether it only runs inside a DO $$ ... END$$; block*
        # at all. A statement sitting outside every DO block executes
        # unconditionally at the top level of the file, which is exactly
        # the bug this test guards against.
        enclosing = None
        for start, end, block_text in do_blocks:
            if start <= pos <= end:
                enclosing = block_text
                break

        if enclosing is None:
            failures.append(
                f"{path.name}: unguarded 'ALTER TABLE {target} ADD CONSTRAINT {conname}' "
                "runs unconditionally at the top level (will fail with a duplicate-"
                "constraint error on re-deployment)"
            )
            continue

        if not CONDITIONAL_GUARD_RE.search(enclosing):
            failures.append(
                f"{path.name}: guard block for '{conname}' has no conditional existence "
                "check (expected 'IF NOT EXISTS (...)' or an equivalent 'IF NOT <flag> THEN')"
            )
            continue

        is_fk = bool(FOREIGN_KEY_RE.search(cleaned[pos : pos + 400]))

        has_schema_scope = bool(SCHEMA_SCOPE_RE.search(enclosing))
        has_table_scope = bool(
            re.search(rf"relname\s*=\s*'{re.escape(table_name)}'", enclosing)
        )
        has_conname_scope = bool(
            re.search(rf"conname\s*=\s*'{re.escape(conname)}'", enclosing)
        )

        if not has_schema_scope:
            failures.append(
                f"{path.name}: guard for '{conname}' on {table_name} is not scoped by schema "
                "(expected e.g. n.nspname = :'schema')"
            )
            continue
        if not has_table_scope:
            failures.append(
                f"{path.name}: guard for '{conname}' is not scoped by owning table "
                f"(expected r.relname = '{table_name}')"
            )
            continue

        if is_fk and not has_conname_scope:
            failures.append(
                f"{path.name}: foreign-key guard for '{conname}' on {table_name} is not "
                f"scoped by constraint name (expected c.conname = '{conname}'); a table can "
                "have more than one foreign key, so table+schema alone is not enough"
            )
            continue

        guarded.append((conname, "fk" if is_fk else "other"))

    return failures, guarded


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    default_repo_root = script_dir.parent.parent
    repo_root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else default_repo_root

    tables_dir = repo_root / "db" / "tables"
    if not tables_dir.is_dir():
        print(f"FAIL: expected directory not found: {tables_dir}", file=sys.stderr)
        return 1

    sql_files = sorted(tables_dir.glob("*.sql")) + sorted((tables_dir / "terminology").glob("*.sql"))

    all_failures: list[str] = []
    found_fk: set[tuple[str, str]] = set()
    total_guarded = 0
    total_fk = 0

    for path in sql_files:
        failures, guarded = analyze_file(path)
        all_failures.extend(failures)
        total_guarded += len(guarded)
        for conname, kind in guarded:
            if kind == "fk":
                total_fk += 1
                found_fk.add((path.name, conname))

    missing = EXPECTED_FK_CONSTRAINTS - found_fk
    if missing:
        missing_list = "\n".join(f"  - {f}: {c}" for f, c in sorted(missing))
        all_failures.append(
            "expected foreign-key constraints were not discovered as guarded "
            f"(parser may have found nothing, or a guard regressed):\n{missing_list}"
        )

    if all_failures:
        print("FAIL: constraint idempotency guard violations found:\n", file=sys.stderr)
        for f in all_failures:
            print(f"  - {f}", file=sys.stderr)
        print(f"\n{len(all_failures)} failure(s).", file=sys.stderr)
        return 1

    print(
        "PASS: all active ALTER TABLE ... ADD CONSTRAINT statements under db/tables/ "
        "are guarded by a schema/table"
        + ("/constraint-name" if total_fk else "")
        + "-scoped existence check."
    )
    print(
        f"      Checked {len(sql_files)} SQL file(s); "
        f"{total_guarded} guarded constraint(s) total, "
        f"{total_fk} of them foreign keys "
        f"(all {len(EXPECTED_FK_CONSTRAINTS)} expected core foreign keys were found)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
