"""Unit tests for tuva_ingest.identifiers -- the single, authoritative
validation policy for dynamic PostgreSQL identifiers (schema and relation
names) used anywhere in this repository.

Covers: every valid-identifier shape the policy accepts, every hostile /
malformed input class the policy must reject (SQL injection shapes,
whitespace, comments, quotes, null bytes, non-ASCII lookalikes, wrong
types), full-match semantics (no partial/prefix matches), no silent
normalization, and that error messages include the caller-supplied label
without leaking anything beyond the rejected value itself.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.identifiers import (  # noqa: E402
    IDENTIFIER_PATTERN,
    InvalidIdentifierError,
    validate_identifier,
    validate_identifiers,
)


class TestValidIdentifiers(unittest.TestCase):
    def test_simple_lowercase(self):
        self.assertEqual(validate_identifier("tuva", "schema"), "tuva")

    def test_with_underscore(self):
        self.assertEqual(validate_identifier("tuva_ops", "schema"), "tuva_ops")

    def test_leading_underscore(self):
        self.assertEqual(validate_identifier("_temporary", "schema"), "_temporary")

    def test_mixed_case_with_digits(self):
        self.assertEqual(validate_identifier("TestSchema_1", "schema"), "TestSchema_1")

    def test_single_character(self):
        self.assertEqual(validate_identifier("a", "schema"), "a")

    def test_single_underscore(self):
        self.assertEqual(validate_identifier("_", "schema"), "_")

    def test_digit_after_first_char(self):
        self.assertEqual(validate_identifier("a1", "schema"), "a1")

    def test_all_uppercase(self):
        self.assertEqual(validate_identifier("SCHEMA", "schema"), "SCHEMA")

    def test_long_name(self):
        name = "a" + ("b" * 100)
        self.assertEqual(validate_identifier(name, "schema"), name)

    def test_returns_exact_value_no_normalization(self):
        # Case must be preserved exactly -- never lowercased.
        self.assertEqual(validate_identifier("MixedCase", "schema"), "MixedCase")

    def test_no_trimming_of_valid_value(self):
        # A valid identifier with no surrounding whitespace round-trips
        # unchanged (whitespace itself is covered under invalid cases).
        self.assertEqual(validate_identifier("tuva", "schema"), "tuva")


class TestInvalidIdentifiers(unittest.TestCase):
    def _assert_rejected(self, value, label="schema"):
        with self.assertRaises(InvalidIdentifierError):
            validate_identifier(value, label)

    def test_empty_string(self):
        self._assert_rejected("")

    def test_none(self):
        self._assert_rejected(None)

    def test_integer(self):
        self._assert_rejected(123)

    def test_bytes(self):
        self._assert_rejected(b"tuva")

    def test_list(self):
        self._assert_rejected(["tuva"])

    def test_leading_digit(self):
        self._assert_rejected("1schema")

    def test_hyphen(self):
        self._assert_rejected("schema-name")

    def test_dot_qualified(self):
        self._assert_rejected("schema.name")

    def test_internal_whitespace(self):
        self._assert_rejected("schema name")

    def test_leading_whitespace(self):
        self._assert_rejected(" schema")

    def test_trailing_whitespace(self):
        self._assert_rejected("schema ")

    def test_tab(self):
        self._assert_rejected("schema\tname")

    def test_newline(self):
        self._assert_rejected("schema\n")

    def test_newline_after_valid_prefix_not_accepted_via_partial_match(self):
        # Guards against re.match()-with-$ trailing-newline gotcha: a
        # value that is a valid identifier plus a trailing newline must
        # still be rejected outright (full-match semantics).
        self._assert_rejected("tuva\n")

    def test_carriage_return(self):
        self._assert_rejected("schema\r")

    def test_semicolon_injection_shape(self):
        self._assert_rejected("schema; DROP TABLE patient")

    def test_double_quote(self):
        self._assert_rejected('schema"name')

    def test_single_quote(self):
        self._assert_rejected("schema'name")

    def test_line_comment(self):
        self._assert_rejected("schema--comment")

    def test_block_comment(self):
        self._assert_rejected("schema/*comment*/")

    def test_null_byte(self):
        self._assert_rejected("schema\x00name")

    def test_backslash(self):
        self._assert_rejected("schema\\name")

    def test_percent_placeholder(self):
        self._assert_rejected("schema%s")

    def test_braces(self):
        self._assert_rejected("schema{}")

    def test_parentheses(self):
        self._assert_rejected("schema()")

    def test_unicode_lookalike(self):
        self._assert_rejected("café")

    def test_non_ascii_word(self):
        self._assert_rejected("schéma")

    def test_dollar_sign(self):
        self._assert_rejected("schema$1")

    def test_only_whitespace(self):
        self._assert_rejected("   ")

    def test_prefix_match_not_accepted(self):
        # A value that merely starts with a valid identifier but has
        # trailing garbage must be rejected -- proves full-match, not
        # re.match()/search() partial-match, semantics.
        self._assert_rejected("tuva; SELECT 1")


class TestErrorMessageContent(unittest.TestCase):
    def test_label_appears_in_error(self):
        with self.assertRaises(InvalidIdentifierError) as ctx:
            validate_identifier("bad name", "ops_schema")
        self.assertIn("ops_schema", str(ctx.exception))

    def test_different_label_for_different_field(self):
        with self.assertRaises(InvalidIdentifierError) as ctx:
            validate_identifier("bad name", "PG_SCHEMA")
        self.assertIn("PG_SCHEMA", str(ctx.exception))

    def test_exception_exposes_label_and_value_attributes(self):
        with self.assertRaises(InvalidIdentifierError) as ctx:
            validate_identifier("bad name", "table")
        self.assertEqual(ctx.exception.label, "table")
        self.assertEqual(ctx.exception.value, "bad name")

    def test_is_a_value_error_subclass(self):
        # Callers that catch ValueError broadly must still catch this.
        self.assertTrue(issubclass(InvalidIdentifierError, ValueError))

    def test_error_does_not_silently_truncate_reported_value(self):
        hostile = "schema; DROP TABLE patient; --"
        with self.assertRaises(InvalidIdentifierError) as ctx:
            validate_identifier(hostile, "schema")
        self.assertIn(hostile, str(ctx.exception))


class TestValidateIdentifiers(unittest.TestCase):
    def test_multiple_valid_returned_in_order(self):
        result = validate_identifiers(("tuva", "schema"), ("pipeline_runs", "table"))
        self.assertEqual(result, ("tuva", "pipeline_runs"))

    def test_schema_and_table_validated_independently(self):
        # A valid schema does not mask an invalid table, and vice versa.
        with self.assertRaises(InvalidIdentifierError) as ctx:
            validate_identifiers(("tuva", "schema"), ("bad table", "table"))
        self.assertIn("table", str(ctx.exception))

    def test_first_invalid_pair_raises_even_if_second_is_valid(self):
        with self.assertRaises(InvalidIdentifierError) as ctx:
            validate_identifiers(("bad schema", "schema"), ("pipeline_runs", "table"))
        self.assertIn("schema", str(ctx.exception))

    def test_empty_call_returns_empty_tuple(self):
        self.assertEqual(validate_identifiers(), ())


class TestIdentifierPattern(unittest.TestCase):
    """Direct checks on the compiled pattern itself, since validate_identifier
    must use fullmatch() semantics against it (not match() or search())."""

    def test_pattern_fullmatch_rejects_trailing_newline(self):
        self.assertIsNone(IDENTIFIER_PATTERN.fullmatch("tuva\n"))

    def test_pattern_fullmatch_accepts_valid(self):
        self.assertIsNotNone(IDENTIFIER_PATTERN.fullmatch("tuva_ops"))

    def test_pattern_fullmatch_rejects_embedded_garbage(self):
        self.assertIsNone(IDENTIFIER_PATTERN.fullmatch("tuva; DROP TABLE x"))


if __name__ == "__main__":
    unittest.main()
