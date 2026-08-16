#!/usr/bin/env python3
"""Static regression guard against raw dynamic-schema-name interpolation
in SQL execution paths.

This does not replace the runtime enforcement in src/tuva_postgres/
identifiers.py (a hostile schema name is rejected there, and again by
src/tuva_postgres/db.py's composition helpers, no matter what this script
finds) -- it exists to catch a *regression* early: someone reintroducing
`f"... {ops_schema} ..."`/`"... %s ..." % (schema,)`/`"...".format(schema)`
directly into a `cursor.execute(...)` call, or reinventing a private
manual-string-quoting identifier helper outside db.py, in a follow-up
change or a different branch's utility script.

This is an AST-based scan (using Python's own `ast` module), not a broad
grep -- it only inspects the first argument of calls whose attribute name
is exactly `execute` (i.e., `<cursor-like>.execute(...)`), and only flags
two narrow, well-understood shapes:

  1. The raw schema-name parameter (`ops_schema`, `pg_schema`,
     `terminology_schema`, or a bare `schema`, including `self.`/`config.`-
     qualified reads of the same names) appearing directly inside an
     f-string, `%`-formatted string, or `.format(...)` call passed as
     query text -- instead of going through one of this repository's
     validated composition helpers (`qualified_relation`, `quote_ident`,
     `identifier_sql`, `qualified_identifier_sql`, `validated_identifier`,
     or their `db.`-qualified forms) first. A validated, quoted value is
     always assigned to a *differently*-named variable in this codebase
     (`relation`, `history_relation`, `schema_ident`, `test_results`,
     ...) specifically so this exact-name check can tell safe composition
     apart from raw interpolation without needing real dataflow analysis.
  2. Any `%`-operator or `.format(...)` call used to build the query text
     argument of `.execute(...)` at all -- this repository's real code
     never does either (it uses f-strings for composed identifiers and
     plain `%s` parameter placeholders, bound via `execute(sql, params)`,
     for data values), so both shapes are inherently suspicious here
     regardless of content.

It also flags manual identifier-quoting-by-string-concatenation (a
`'"' + name + '"'`-shaped `BinOp`) anywhere outside src/tuva_postgres/
db.py itself, which owns the one, canonical `quote_ident()` implementation
-- guarding against a private, divergent re-implementation creeping back
in (this is exactly the shape ops.py's old `_q()` helper used to have).

Scope: src/tuva_postgres/**/*.py, the top-level scripts/*.py (not
scripts/tests/*.py, which script fake stubs/assertions rather than real
SQL execution), and tests/integration/*.py (the one test suite that
executes real SQL against dynamic schema names). A small, explicit
ALLOWLIST below exists for the rare, deliberately-reviewed exception; it
is not a broad carve-out.

Negative-control fixtures (synthetic, in-memory source snippets exercising
every pattern this scanner is meant to catch) are run first, so this
script cannot silently pass by scanning nothing meaningful -- if any
negative control fails to be flagged, this script itself fails loudly
before it ever reports on the real repository.

Standard library only -- no PostgreSQL, network, or third-party
dependencies required.

Usage:
    python3 scripts/tests/test_no_raw_schema_interpolation.py [repo_root]
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[2]

# Raw, unvalidated/unquoted schema-parameter names this repository uses
# throughout config.py/db.py/ops.py/migrations.py/scripts/*.py. Matched
# against the *entire* unparsed expression text of an f-string
# FormattedValue, so `ops_schema`, `self.ops_schema`, `config.pg_schema`,
# etc. all match, but a composed value like `qualified_relation(...)` or a
# differently-named variable like `relation`/`schema_ident` does not.
_RAW_SCHEMA_NAME_RE = re.compile(
    r"(^|\.)(ops_schema|pg_schema|terminology_schema|schema)$", re.IGNORECASE
)

# Explicit, reviewed exceptions: (path relative to repo root, lineno).
# Empty by design -- add an entry here only with a comment explaining
# exactly why that one call site is safe despite matching a flagged
# pattern, never to silence a real finding.
ALLOWLIST: frozenset[tuple[str, int]] = frozenset()

_SAFE_COMPOSITION_CALL_RE = re.compile(
    r"^(db\.)?(qualified_relation|quote_ident|identifier_sql|qualified_identifier_sql|validated_identifier)\("
)


class Violation:
    def __init__(self, path: Path, lineno: int, message: str):
        self.path = path
        self.lineno = lineno
        self.message = message

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno}: {self.message}"

    def key(self, repo_root: Path) -> tuple[str, int]:
        return (str(self.path.relative_to(repo_root)), self.lineno)


def _is_execute_call(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Attribute) and node.func.attr == "execute" and bool(node.args)


def _scan_execute_query_arg(path: Path, node: ast.Call, violations: list[Violation]) -> None:
    query_arg = node.args[0]

    if isinstance(query_arg, ast.JoinedStr):
        for value in query_arg.values:
            if not isinstance(value, ast.FormattedValue):
                continue
            expr_text = ast.unparse(value.value)
            if _RAW_SCHEMA_NAME_RE.search(expr_text) and not _SAFE_COMPOSITION_CALL_RE.match(expr_text):
                violations.append(
                    Violation(
                        path,
                        node.lineno,
                        f"cursor.execute(...) f-string interpolates raw schema-name expression "
                        f"{expr_text!r} directly -- validate and compose it first (see "
                        f"tuva_postgres.db.qualified_relation/quote_ident) instead of embedding the "
                        f"unvalidated schema parameter itself",
                    )
                )
        return

    if isinstance(query_arg, ast.BinOp) and isinstance(query_arg.op, ast.Mod):
        violations.append(
            Violation(
                path,
                node.lineno,
                "cursor.execute(...) query text is built with the '%' string-formatting operator -- "
                "this repository never builds SQL text this way (data values are bound via "
                "execute(sql, params) with plain %s placeholders; dynamic identifiers are composed "
                "via tuva_postgres.db's validated helpers) -- this shape is disallowed regardless of "
                "content",
            )
        )
        return

    if isinstance(query_arg, ast.Call) and isinstance(query_arg.func, ast.Attribute) and query_arg.func.attr == "format":
        violations.append(
            Violation(
                path,
                node.lineno,
                "cursor.execute(...) query text is built with str.format(...) -- this repository "
                "never builds SQL text this way -- this shape is disallowed regardless of content",
            )
        )
        return


def _scan_manual_quote_concatenation(path: Path, tree: ast.AST, violations: list[Violation]) -> None:
    """Flag `'"' + x + '"'`-shaped string concatenation anywhere outside
    db.py (the one canonical home for `quote_ident`) -- guards against a
    private, divergent re-implementation of identifier quoting."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)):
            continue
        operands = [node.left, node.right]
        has_quote_literal = any(
            isinstance(o, ast.Constant) and isinstance(o.value, str) and '"' in o.value for o in operands
        )
        if has_quote_literal:
            violations.append(
                Violation(
                    path,
                    node.lineno,
                    "manual string concatenation involving a literal double-quote character -- looks "
                    "like a private identifier-quoting helper; use tuva_postgres.db.quote_ident (the "
                    "single canonical implementation) instead",
                )
            )


def _iter_source_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    files.extend(sorted((repo_root / "src" / "tuva_postgres").rglob("*.py")))
    scripts_dir = repo_root / "scripts"
    if scripts_dir.is_dir():
        files.extend(sorted(scripts_dir.glob("*.py")))
    integration_dir = repo_root / "tests" / "integration"
    if integration_dir.is_dir():
        files.extend(sorted(integration_dir.glob("*.py")))
    return files


def scan_file(path: Path, *, skip_manual_quote_check: bool = False) -> list[Violation]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [Violation(path, 0, f"could not read file: {exc}")]

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [Violation(path, exc.lineno or 0, f"could not parse file: {exc}")]

    violations: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_execute_call(node):
            _scan_execute_query_arg(path, node, violations)

    if not skip_manual_quote_check:
        _scan_manual_quote_concatenation(path, tree, violations)

    return violations


def scan_repo(repo_root: Path) -> list[Violation]:
    all_violations: list[Violation] = []
    db_py = (repo_root / "src" / "tuva_postgres" / "db.py").resolve()
    for path in _iter_source_files(repo_root):
        skip_manual_quote_check = path.resolve() == db_py
        found = scan_file(path, skip_manual_quote_check=skip_manual_quote_check)
        for v in found:
            if v.key(repo_root) in ALLOWLIST:
                continue
            all_violations.append(v)
    return all_violations


# --- Negative controls: synthetic snippets this scanner MUST flag ----------

_NEGATIVE_CONTROLS: dict[str, str] = {
    "fstring_raw_ops_schema": (
        "def f(cur, ops_schema, run_id):\n"
        '    cur.execute(f"SELECT * FROM {ops_schema}.pipeline_runs WHERE run_id = %s", (run_id,))\n'
    ),
    "fstring_raw_pg_schema_qualified_table": (
        "def f(cur, pg_schema, table):\n"
        '    cur.execute(f\'SELECT COUNT(*) FROM "{pg_schema}"."{table}"\')\n'
    ),
    "fstring_raw_self_ops_schema": (
        "class X:\n"
        "    def f(self, cur):\n"
        '        cur.execute(f"DROP SCHEMA IF EXISTS {self.ops_schema} CASCADE")\n'
    ),
    "percent_formatting": (
        "def f(cur, schema):\n"
        '    cur.execute("SELECT * FROM %s.pipeline_runs" % schema)\n'
    ),
    "str_format": (
        "def f(cur, schema):\n"
        '    cur.execute("SELECT * FROM {}.pipeline_runs".format(schema))\n'
    ),
    "manual_quote_concatenation": (
        "def quote(name):\n"
        "    return '\"' + name + '\"'\n"
    ),
}

# Patterns the scanner MUST NOT flag (safe composition / unrelated code) --
# proves the guard isn't so broad it also rejects legitimate code.
_POSITIVE_CONTROLS: dict[str, str] = {
    "safe_qualified_relation": (
        "def f(cur, ops_schema, run_id):\n"
        "    relation = qualified_relation(ops_schema, 'pipeline_runs', schema_label='ops_schema')\n"
        '    cur.execute(f"SELECT * FROM {relation} WHERE run_id = %s", (run_id,))\n'
    ),
    "safe_quote_ident": (
        "def f(cur, ops_schema):\n"
        "    schema_ident = quote_ident(validated_identifier(ops_schema, 'ops_schema'))\n"
        '    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema_ident}")\n'
    ),
    "ordinary_parameterized_value_query": (
        "def f(cur, pg_schema, table):\n"
        '    cur.execute("SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND '
        'table_name = %s", (pg_schema, table))\n'
    ),
    "non_sql_execute_unrelated": (
        "def f(job):\n"
        "    job.execute()\n"
    ),
}


def run_negative_and_positive_controls() -> list[str]:
    results: list[str] = []
    for name, source in _NEGATIVE_CONTROLS.items():
        tree = ast.parse(source, filename=f"<negative-control:{name}>")
        violations: list[Violation] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_execute_call(node):
                _scan_execute_query_arg(Path(f"<negative-control:{name}>"), node, violations)
        _scan_manual_quote_concatenation(Path(f"<negative-control:{name}>"), tree, violations)
        if not violations:
            raise AssertionError(
                f"negative control {name!r} was NOT flagged by the scanner -- the detector is not "
                "working; source:\n" + source
            )
        results.append(f"negative control {name!r} correctly flagged ({len(violations)} violation(s))")

    for name, source in _POSITIVE_CONTROLS.items():
        tree = ast.parse(source, filename=f"<positive-control:{name}>")
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_execute_call(node):
                _scan_execute_query_arg(Path(f"<positive-control:{name}>"), node, violations)
        _scan_manual_quote_concatenation(Path(f"<positive-control:{name}>"), tree, violations)
        if violations:
            raise AssertionError(
                f"positive control {name!r} (known-safe code) was incorrectly flagged: "
                f"{[str(v) for v in violations]}; source:\n{source}"
            )
        results.append(f"positive control {name!r} correctly left unflagged")

    return results


def main(argv: list[str]) -> int:
    repo_root = Path(argv[1]).resolve() if len(argv) > 1 else REPO_ROOT_DEFAULT

    try:
        control_results = run_negative_and_positive_controls()
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    violations = scan_repo(repo_root)
    if violations:
        print(f"FAIL: {len(violations)} raw schema-interpolation violation(s) found:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    print(
        "PASS: no raw dynamic-schema-name interpolation found in cursor.execute(...) calls, and no "
        "manual identifier-quoting-by-concatenation found outside tuva_postgres/db.py."
    )
    for r in control_results:
        print(f"  - {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
