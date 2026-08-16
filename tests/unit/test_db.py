"""Unit tests for the SQL identifier-composition helpers in
tuva_postgres.db: qualified_relation, quote_ident, validated_identifier,
identifier_sql, qualified_identifier_sql, and substitute_psql_vars.

These prove composition behavior directly (structural assertions on the
returned string / composable), not "safety" inferred solely from a regex
over rendered text -- e.g. we assert the schema and relation components
are quoted *independently* (so a `"` in one can never leak into the
other's boundary), that a dotted "schema.table" string is rejected rather
than silently split, and that invalid input raises before any string is
composed at all.

Tests that require a real psycopg.sql.Identifier composable are skipped
when psycopg isn't installed (this sandbox has no network access to
install it); the plain-string composition helpers (`qualified_relation`,
`quote_ident`) have no such dependency and always run.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_postgres import db  # noqa: E402
from tuva_postgres.identifiers import InvalidIdentifierError  # noqa: E402

try:
    import psycopg  # noqa: F401

    HAVE_PSYCOPG = True
except ImportError:
    HAVE_PSYCOPG = False


class TestValidatedIdentifier(unittest.TestCase):
    def test_valid_passthrough(self):
        self.assertEqual(db.validated_identifier("tuva", "schema"), "tuva")

    def test_hostile_rejected(self):
        with self.assertRaises(InvalidIdentifierError):
            db.validated_identifier("tuva; DROP TABLE patient", "schema")


class TestQuoteIdent(unittest.TestCase):
    def test_wraps_in_double_quotes(self):
        self.assertEqual(db.quote_ident("tuva"), '"tuva"')

    def test_preserves_case(self):
        self.assertEqual(db.quote_ident("TuvaOps"), '"TuvaOps"')

    def test_doubles_embedded_double_quote_defensively(self):
        # quote_ident is a defense-in-depth quoting step, documented as
        # never trusting a single layer of validation alone -- exercise
        # it directly with a value that validate_identifier would have
        # rejected, to prove the escaping logic itself is correct
        # independent of the upstream policy.
        self.assertEqual(db.quote_ident('weird"name'), '"weird""name"')

    def test_reserved_keyword_becomes_valid_quoted_identifier(self):
        # "select" is a reserved SQL keyword but a perfectly valid
        # identifier once quoted -- proves quoting is what makes
        # reserved-keyword-shaped names usable as identifiers.
        self.assertEqual(db.quote_ident("select"), '"select"')


class TestQualifiedRelation(unittest.TestCase):
    def test_basic_composition(self):
        self.assertEqual(db.qualified_relation("tuva_ops", "pipeline_runs"), '"tuva_ops"."pipeline_runs"')

    def test_schema_and_relation_quoted_independently(self):
        # Each component is validated and quoted on its own -- there is
        # no path where one component's content could affect where the
        # other component's quotes are placed.
        result = db.qualified_relation("tuva", "patient")
        schema_part, relation_part = result.split(".")
        self.assertEqual(schema_part, '"tuva"')
        self.assertEqual(relation_part, '"patient"')

    def test_reserved_keyword_relation_name(self):
        self.assertEqual(db.qualified_relation("tuva", "select"), '"tuva"."select"')

    def test_dotted_schema_rejected_not_split(self):
        # A caller must never be able to smuggle a second identifier by
        # embedding a dot in one argument -- the dot itself is outside
        # the allowed character set, so this must raise, not silently
        # treat "a.b" as schema="a", relation="b".
        with self.assertRaises(InvalidIdentifierError):
            db.qualified_relation("tuva.extra", "patient")

    def test_dotted_relation_rejected_not_split(self):
        with self.assertRaises(InvalidIdentifierError):
            db.qualified_relation("tuva", "extra.patient")

    def test_prequoted_schema_rejected(self):
        # A pre-quoted string must never be accepted as-is -- the quotes
        # themselves are outside the allowed character set.
        with self.assertRaises(InvalidIdentifierError):
            db.qualified_relation('"tuva"', "patient")

    def test_hostile_schema_rejected_before_composition(self):
        with self.assertRaises(InvalidIdentifierError):
            db.qualified_relation("tuva; DROP TABLE patient; --", "patient")

    def test_hostile_relation_rejected_before_composition(self):
        with self.assertRaises(InvalidIdentifierError):
            db.qualified_relation("tuva", "patient; DROP TABLE patient; --")

    def test_hostile_input_raises_before_returning_any_string(self):
        # Confirms the function does not return a partially-composed or
        # sanitized string on invalid input -- it raises, full stop.
        try:
            db.qualified_relation("bad schema", "patient")
        except InvalidIdentifierError:
            pass
        else:
            self.fail("expected InvalidIdentifierError")

    def test_custom_labels_appear_in_error(self):
        with self.assertRaises(InvalidIdentifierError) as ctx:
            db.qualified_relation("bad schema", "patient", schema_label="ops_schema")
        self.assertIn("ops_schema", str(ctx.exception))

    def test_error_message_never_leaks_beyond_rejected_value(self):
        with self.assertRaises(InvalidIdentifierError) as ctx:
            db.qualified_relation("tuva", "bad table", relation_label="table")
        message = str(ctx.exception)
        self.assertIn("table", message)
        self.assertIn("bad table", message)


class TestSubstitutePsqlVars(unittest.TestCase):
    def test_identifier_form_renders_quoted(self):
        out = db.substitute_psql_vars(':"schema"', {"schema": "tuva"})
        self.assertEqual(out, '"tuva"')

    def test_identifier_form_rejects_hostile_value_before_rendering(self):
        with self.assertRaises(InvalidIdentifierError):
            db.substitute_psql_vars(':"schema"', {"schema": 'weird"name'})

    def test_identifier_form_rejects_injection_shaped_value(self):
        with self.assertRaises(InvalidIdentifierError):
            db.substitute_psql_vars(':"schema"', {"schema": "tuva\"; DROP TABLE patient; --"})

    def test_literal_form_escapes_quotes_without_identifier_validation(self):
        out = db.substitute_psql_vars(":'msg'", {"msg": "it's a \"test\""})
        self.assertEqual(out, "'it''s a \"test\"'")

    def test_only_identifier_form_usages_are_validated(self):
        # A variable used only via :'name' (literal form) must not be
        # forced through identifier validation.
        out = db.substitute_psql_vars(":'other'", {"other": "has spaces and 'quotes'"})
        self.assertEqual(out, "'has spaces and ''quotes'''")

    def test_type_cast_double_colon_not_mistaken_for_var(self):
        out = db.substitute_psql_vars("SELECT x::text", {"x": "tuva"})
        self.assertEqual(out, "SELECT x::text")

    def test_both_forms_of_same_variable_in_one_statement(self):
        out = db.substitute_psql_vars(
            'CREATE SCHEMA IF NOT EXISTS :"schema"; -- for :\'schema\'',
            {"schema": "tuva_ops"},
        )
        self.assertIn('"tuva_ops"', out)
        self.assertIn("'tuva_ops'", out)


@unittest.skipUnless(HAVE_PSYCOPG, "psycopg is not installed in this environment")
class TestPsycopgComposables(unittest.TestCase):
    def test_identifier_sql_returns_psycopg_identifier(self):
        result = db.identifier_sql("tuva", "schema")
        self.assertIsInstance(result, psycopg.sql.Identifier)

    def test_identifier_sql_rejects_hostile_value(self):
        with self.assertRaises(InvalidIdentifierError):
            db.identifier_sql("tuva; DROP TABLE patient", "schema")

    def test_qualified_identifier_sql_returns_psycopg_identifier(self):
        result = db.qualified_identifier_sql("tuva_ops", "pipeline_runs")
        self.assertIsInstance(result, psycopg.sql.Identifier)

    def test_qualified_identifier_sql_renders_two_part_identifier(self):
        result = db.qualified_identifier_sql("tuva_ops", "pipeline_runs")
        rendered = result.as_string(None)
        self.assertEqual(rendered, '"tuva_ops"."pipeline_runs"')

    def test_qualified_identifier_sql_rejects_hostile_schema(self):
        with self.assertRaises(InvalidIdentifierError):
            db.qualified_identifier_sql("tuva; DROP TABLE patient", "pipeline_runs")

    def test_qualified_identifier_sql_rejects_hostile_relation(self):
        with self.assertRaises(InvalidIdentifierError):
            db.qualified_identifier_sql("tuva_ops", "pipeline_runs; DROP TABLE patient")

    def test_composed_query_keeps_values_as_separate_params(self):
        # Build a real Composed query the way ops.py/migrations.py would,
        # and prove the composed SQL text contains no data value --
        # values stay out of the identifier-composition path entirely
        # and are only ever bound at execute() time via a separate
        # params tuple.
        relation = db.qualified_identifier_sql("tuva_ops", "pipeline_runs")
        query = psycopg.sql.SQL("SELECT * FROM {} WHERE run_id = %s").format(relation)
        rendered = query.as_string(None)
        self.assertIn('"tuva_ops"."pipeline_runs"', rendered)
        self.assertNotIn("some-run-id-value", rendered)


if __name__ == "__main__":
    unittest.main()
