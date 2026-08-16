"""Database-free, network-free validation of the source-to-Tuva claims
mapping: `docs/CLAIMS_MAPPING.csv`, `docs/CLAIMS_MAPPING_DECISIONS.md`,
and the executable transformation/DQ logic in `src/tuva_ingest/
claims_mapping.py`, exercised against the synthetic representative
sample under `tests/fixtures/claims_mapping_sample/`.

This is the readiness gate the task requires: a full historical
ingestion must not be treated as approved until
`claims_mapping.historical_ingestion_ready()` returns `ready=True`, and
that can only happen once every rule documented in `docs/
CLAIMS_MAPPING_DECISIONS.md` passes against the representative sample
and the mapping sheet/decisions doc are free of unresolved
placeholders. Nothing in this module (or in `claims_mapping.py`) reads
`RAW_DATA_DIR`, the `raw`/`ingest_ops` schemas, or performs any network
or database I/O -- it runs everywhere `make test-unit` runs, matching
the style of `tests/unit/test_input_layer_contract.py` and
`tests/unit/test_source_contract.py`.

Every failure message below names the affected source field, Tuva
field, and failed rule, so a broken mapping fails loudly and
specifically.
"""
from __future__ import annotations

import csv
import re
import sys
import unittest
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest import claims_mapping as cm  # noqa: E402

MAPPING_SHEET_PATH = REPO_ROOT / "docs" / "CLAIMS_MAPPING.csv"
DECISIONS_DOC_PATH = REPO_ROOT / "docs" / "CLAIMS_MAPPING_DECISIONS.md"

REQUIRED_COLUMNS = ["Source field", "Source meaning", "Tuva model/field", "Transformation", "Required?", "DQ rule"]

# (Source field, Tuva model/field) -- the four mappings the task
# mandates verbatim (paid_cents/clm_id/line_no fully qualified per this
# repository's existing fully-qualified-column-name convention, see
# models/final/schema.yml; member_key qualified to eligibility.person_id,
# its verified primary-key-owning model -- see docs/
# CLAIMS_MAPPING_DECISIONS.md decision 5).
REQUIRED_MAPPING_ROWS = [
    ("clm_id", "medical_claim.claim_id"),
    ("line_no", "medical_claim.claim_line_number"),
    ("member_key", "eligibility.person_id"),
    ("paid_cents", "medical_claim.paid_amount"),
]


def _read_mapping_rows() -> list[dict]:
    with open(MAPPING_SHEET_PATH, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _read_mapping_header() -> list[str]:
    with open(MAPPING_SHEET_PATH, newline="", encoding="utf-8") as fh:
        return next(csv.reader(fh))


class TestMappingSheetExistsAndIsWellFormed(unittest.TestCase):
    def test_mapping_sheet_exists(self):
        self.assertTrue(MAPPING_SHEET_PATH.is_file(), "docs/CLAIMS_MAPPING.csv must exist")

    def test_decisions_doc_exists(self):
        self.assertTrue(DECISIONS_DOC_PATH.is_file(), "docs/CLAIMS_MAPPING_DECISIONS.md must exist")

    def test_required_columns_present_in_required_order(self):
        header = _read_mapping_header()
        self.assertEqual(
            header, REQUIRED_COLUMNS,
            f"docs/CLAIMS_MAPPING.csv header {header} must equal {REQUIRED_COLUMNS} in this exact order",
        )

    def test_csv_parses_and_every_row_has_six_fields(self):
        rows = _read_mapping_rows()
        self.assertGreater(len(rows), 0)
        for i, row in enumerate(rows):
            with self.subTest(row=i):
                self.assertEqual(
                    set(row.keys()), set(REQUIRED_COLUMNS), f"row {i} does not have exactly the six required columns"
                )


def _find_row(rows: list[dict], source_field: str, tuva_field: str) -> dict:
    return next(r for r in rows if r["Source field"] == source_field and r["Tuva model/field"] == tuva_field)


class TestRequiredMappingRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = _read_mapping_rows()

    def test_each_required_row_exists_exactly_once(self):
        for source_field, tuva_field in REQUIRED_MAPPING_ROWS:
            with self.subTest(source_field=source_field, tuva_field=tuva_field):
                matches = [
                    r for r in self.rows
                    if r["Source field"] == source_field and r["Tuva model/field"] == tuva_field
                ]
                self.assertEqual(
                    len(matches), 1,
                    f"expected exactly one docs/CLAIMS_MAPPING.csv row for source field {source_field!r} -> "
                    f"{tuva_field!r}, found {len(matches)}",
                )

    def test_required_rows_have_non_empty_required_fields(self):
        for source_field, tuva_field in REQUIRED_MAPPING_ROWS:
            row = _find_row(self.rows, source_field, tuva_field)
            with self.subTest(source_field=source_field):
                for column in ("Source meaning", "Tuva model/field", "Transformation", "Required?", "DQ rule"):
                    self.assertTrue(
                        row[column] and row[column].strip(),
                        f"docs/CLAIMS_MAPPING.csv row for {source_field!r} -> {tuva_field!r} has an empty {column!r}",
                    )

    def test_required_flags_match_task_specification(self):
        expected = {"clm_id": "Yes", "line_no": "Yes", "member_key": "Yes", "paid_cents": "No"}
        for source_field, tuva_field in REQUIRED_MAPPING_ROWS:
            row = _find_row(self.rows, source_field, tuva_field)
            with self.subTest(source_field=source_field):
                self.assertEqual(
                    row["Required?"], expected[source_field],
                    f"{source_field} -> {tuva_field}: Required? must be {expected[source_field]!r}",
                )

    def test_dq_rules_match_task_specification(self):
        expected_substring = {
            "clm_id": "not null",
            "line_no": "unique",
            "member_key": "fk coverage",
            "paid_cents": "range",
        }
        for source_field, tuva_field in REQUIRED_MAPPING_ROWS:
            row = _find_row(self.rows, source_field, tuva_field)
            with self.subTest(source_field=source_field):
                self.assertIn(expected_substring[source_field], row["DQ rule"].lower())


class TestNoUnresolvedPlaceholders(unittest.TestCase):
    def test_mapping_sheet_has_no_placeholders(self):
        text = MAPPING_SHEET_PATH.read_text(encoding="utf-8")
        found = cm.find_placeholders(text)
        self.assertEqual(found, [], f"docs/CLAIMS_MAPPING.csv contains unresolved placeholder(s): {found}")

    def test_decisions_doc_has_no_placeholders(self):
        text = DECISIONS_DOC_PATH.read_text(encoding="utf-8")
        found = cm.find_placeholders(text)
        self.assertEqual(found, [], f"docs/CLAIMS_MAPPING_DECISIONS.md contains unresolved placeholder(s): {found}")

    def test_placeholder_scan_actually_detects_injected_placeholders(self):
        # Sanity check on the scanner itself: prove it is not vacuously
        # passing by feeding it text that must trip every marker.
        sample_text = "TBD: fill this in later. TODO: unknown vendor. FIXME lorem ipsum XXX"
        found = cm.find_placeholders(sample_text)
        self.assertGreaterEqual(
            len(found), 5, f"placeholder scanner under-detected on a deliberately bad sample: {found}"
        )


class _RepresentativeSampleTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.crosswalk = cm.load_crosswalk()
        cls.claim_rows = cm._load_rows(cm.CLAIMS_SAMPLE_PATH)
        cls.eligibility_rows = cm._load_rows(cm.ELIGIBILITY_SAMPLE_PATH)
        cls.result = cm.transform_claims(cls.claim_rows, cls.crosswalk)
        cls.eligibility_result = cm.consolidate_eligibility(cls.eligibility_rows, cls.crosswalk)


class TestClmIdMapping(_RepresentativeSampleTestCase):
    def test_clm_id_maps_to_medical_claim_claim_id_and_is_non_null_after_trim(self):
        for line in self.result.accepted:
            with self.subTest(claim_id=line.claim_id):
                self.assertIsNotNone(line.claim_id)
                self.assertEqual(line.claim_id, line.claim_id.strip())
                self.assertNotEqual(line.claim_id, "")

    def test_blank_clm_id_is_rejected_not_defaulted(self):
        self.assertTrue(
            any(r.field == "clm_id" for r in self.result.rejected),
            "the representative sample's blank-clm_id row must be rejected, not silently defaulted",
        )


class TestLineNoCasting(_RepresentativeSampleTestCase):
    def test_line_no_casts_safely_to_integer(self):
        for line in self.result.accepted:
            with self.subTest(claim_id=line.claim_id, line_no=line.line_no):
                self.assertIsInstance(line.line_no, int)

    def test_unparseable_line_no_is_rejected_not_coerced(self):
        rejected = [r for r in self.result.rejected if r.field == "line_no"]
        self.assertEqual(
            len(rejected), 1, "expected exactly one line_no rejection (clm-1010's 'X') in the representative sample"
        )
        self.assertEqual(rejected[0].raw.get("clm_id"), "clm-1010")


class TestGrainUniqueness(_RepresentativeSampleTestCase):
    def test_no_grain_conflicts_in_representative_sample(self):
        conflicts = cm.find_grain_conflicts(self.result.accepted)
        self.assertEqual(
            conflicts, [], f"(claim_id, claim_line_number, data_source) grain has conflicts: {conflicts}"
        )

    def test_duplicate_delivery_is_collapsed_not_double_loaded(self):
        self.assertEqual(
            self.result.duplicate_rows_collapsed, 1,
            "the byte-identical re-delivered clm-1001 line 1 row must be collapsed exactly once",
        )
        matching = [line for line in self.result.accepted if line.claim_id == "clm-1001" and line.line_no == 1]
        self.assertEqual(
            len(matching), 1,
            "clm-1001 line 1 must appear exactly once in accepted output despite the duplicate delivery",
        )

    def test_conflicting_same_key_rows_would_be_flagged_not_silently_resolved(self):
        # Construct two rows sharing a grain key with DIFFERENT content
        # (not a byte-identical duplicate) and confirm the conflict
        # surfaces rather than one row silently winning.
        conflicting = [
            {"clm_id": "clm-9001", "line_no": "1", "member_key": "mk-001", "clm_status_code": "1", "orig_clm_id": "",
             "bill_type_code": "", "place_of_service_code": "11", "claim_form_code": "CMS1500",
             "service_from_dt": "2025-01-01", "service_to_dt": "2025-01-01", "rev_code": "", "diag_type": "",
             "diag_cd_1": "", "diag_cd_2": "", "diag_cd_3": "", "proc_type": "", "proc_cd_1": "", "proc_dt_1": "",
             "proc_cd_2": "", "proc_dt_2": "", "paid_cents": "1000", "allowed_cents": "1000", "charge_cents": "1000"},
            {"clm_id": "clm-9001", "line_no": "1", "member_key": "mk-001", "clm_status_code": "1", "orig_clm_id": "",
             "bill_type_code": "", "place_of_service_code": "11", "claim_form_code": "CMS1500",
             "service_from_dt": "2025-01-01", "service_to_dt": "2025-01-01", "rev_code": "", "diag_type": "",
             "diag_cd_1": "", "diag_cd_2": "", "diag_cd_3": "", "proc_type": "", "proc_cd_1": "", "proc_dt_1": "",
             "proc_cd_2": "", "proc_dt_2": "", "paid_cents": "9999", "allowed_cents": "9999", "charge_cents": "9999"},
        ]
        result = cm.transform_claims(conflicting, self.crosswalk)
        conflicts = cm.find_grain_conflicts(result.accepted)
        self.assertEqual(
            len(conflicts), 1, "two rows sharing a grain key with different content must be flagged as a conflict"
        )


class TestMemberCrosswalk(_RepresentativeSampleTestCase):
    def test_member_key_resolves_via_documented_crosswalk(self):
        line = next(line for line in self.result.accepted if line.claim_id == "clm-1001")
        self.assertEqual(line.person_id, "person-001")

    def test_merged_member_keys_resolve_to_the_same_person_id(self):
        original = next(line for line in self.result.accepted if line.member_key == "mk-002")
        merged_alias = next(line for line in self.result.accepted if line.member_key == "mk-002b")
        self.assertEqual(
            original.person_id, merged_alias.person_id,
            "mk-002 and mk-002b must resolve to the same person_id (documented merge)",
        )

    def test_unmatched_member_is_flagged_not_dropped(self):
        line = next(line for line in self.result.accepted if line.member_key == "mk-999")
        self.assertFalse(line.matched_member)
        self.assertIsNone(line.person_id)


class TestForeignKeyCoverage(_RepresentativeSampleTestCase):
    def test_fk_coverage_meets_documented_threshold(self):
        member_keys = {line.member_key for line in self.result.accepted}
        member_keys |= {
            cm.trim(row.get("member_key")) for row in self.eligibility_rows if cm.trim(row.get("member_key"))
        }
        matched, total, ratio = cm.fk_coverage(member_keys, self.crosswalk)
        threshold = cm.FK_COVERAGE_THRESHOLD
        self.assertGreaterEqual(
            ratio, threshold,
            f"FK coverage {ratio:.2%} ({matched}/{total}) is below the documented {threshold:.0%} threshold",
        )

    def test_fk_coverage_would_fail_below_threshold(self):
        # Prove the check is not vacuous: a crosswalk covering nothing
        # must fail the same threshold comparison.
        matched, total, ratio = cm.fk_coverage({"mk-001", "mk-999"}, {})
        self.assertLess(ratio, cm.FK_COVERAGE_THRESHOLD)


class TestFinancialUnits(_RepresentativeSampleTestCase):
    def test_paid_cents_converts_to_paid_amount_without_truncation(self):
        self.assertEqual(cm.cents_to_amount("8500"), Decimal("85.00"))
        self.assertEqual(cm.cents_to_amount("450000"), Decimal("4500.00"))

    def test_cents_to_amount_uses_decimal_division_not_integer_truncation(self):
        # 8501 // 100 == 85 (integer division would silently lose a cent).
        self.assertEqual(cm.cents_to_amount("8501"), Decimal("85.01"))
        self.assertNotEqual(cm.cents_to_amount("8501"), Decimal(8501 // 100))

    def test_blank_financial_value_is_null_not_zero(self):
        self.assertIsNone(cm.cents_to_amount(""))
        self.assertIsNone(cm.cents_to_amount(None))

    def test_negative_cents_for_a_void_convert_to_negative_decimal(self):
        self.assertEqual(cm.cents_to_amount("-300000"), Decimal("-3000.00"))

    def test_reconciliation_rule_holds_across_the_representative_sample(self):
        violations = cm.reconciliation_violations(self.result.accepted)
        self.assertEqual(violations, [], f"reconciliation violations: {violations}")

    def test_reconciliation_rule_actually_detects_a_violation(self):
        # Sanity check: paid > allowed must be caught, not silently passed.
        bad_line = cm.TransformedClaimLine(
            claim_id="clm-bad", line_no=1, person_id="person-001", member_key="mk-001", matched_member=True,
            clm_status_code="1", orig_clm_id=None, claim_type="professional", bill_type_code=None,
            place_of_service_code="11", rev_code=None,
            paid_amount=Decimal("500.00"), allowed_amount=Decimal("100.00"), charge_amount=Decimal("600.00"),
        )
        violations = cm.reconciliation_violations([bad_line])
        self.assertEqual(len(violations), 1)
        self.assertIn("clm-bad", violations[0])


class TestAdjustmentsAndReversals(_RepresentativeSampleTestCase):
    def test_adjustment_replaces_original_in_net_total(self):
        lineage = [line for line in self.result.accepted if line.claim_id in ("clm-1002", "clm-1002-adj")]
        self.assertEqual(cm.net_paid_amount(lineage), Decimal("95.00"))

    def test_void_cancels_original_in_net_total(self):
        lineage = [line for line in self.result.accepted if line.claim_id in ("clm-1003", "clm-1003-void")]
        self.assertEqual(cm.net_paid_amount(lineage), Decimal("0.00"))

    def test_original_alone_would_double_count_without_netting(self):
        # Demonstrates why the netting logic matters: a naive sum (no
        # adjustment/void awareness) over the same lineage would NOT
        # equal the netted totals above.
        lineage = [line for line in self.result.accepted if line.claim_id in ("clm-1002", "clm-1002-adj")]
        naive_sum = sum((line.paid_amount for line in lineage if line.paid_amount is not None), Decimal("0"))
        self.assertNotEqual(naive_sum, cm.net_paid_amount(lineage))


class TestClaimTyping(_RepresentativeSampleTestCase):
    def test_every_accepted_line_has_a_recognized_claim_type(self):
        for line in self.result.accepted:
            with self.subTest(claim_id=line.claim_id):
                self.assertIn(line.claim_type, cm.ACCEPTED_CLAIM_TYPES)

    def test_form_code_takes_precedence_over_bill_type_and_pos(self):
        row = {"claim_form_code": "UB04", "bill_type_code": "", "place_of_service_code": "11"}
        self.assertEqual(cm.derive_claim_type(row), "institutional")

    def test_ambiguous_input_resolves_deterministically(self):
        ambiguous = {"claim_form_code": "", "bill_type_code": "041", "place_of_service_code": "12"}
        self.assertEqual(cm.derive_claim_type(ambiguous), "institutional")
        self.assertEqual(
            cm.derive_claim_type(ambiguous), cm.derive_claim_type(ambiguous),
            "claim typing must be deterministic (same input -> same output)",
        )

    def test_missing_typing_fields_resolve_undetermined_not_null(self):
        line = next(line for line in self.result.accepted if line.claim_id == "clm-1012")
        self.assertEqual(line.claim_type, "undetermined")


class TestDiagnosisAndProcedureNormalization(_RepresentativeSampleTestCase):
    def test_sequence_positions_are_unique_and_primary_is_position_one(self):
        line = next(line for line in self.result.accepted if line.claim_id == "clm-1007")
        sequences = [d.sequence for d in line.diagnoses]
        self.assertEqual(sequences, sorted(set(sequences)), "diagnosis sequence numbers must be unique and ascending")
        primaries = [d for d in line.diagnoses if d.is_primary]
        self.assertEqual(len(primaries), 1)
        self.assertEqual(primaries[0].sequence, 1)
        self.assertEqual(primaries[0].code, "E11.9")

    def test_duplicate_diagnosis_codes_are_deduplicated(self):
        line = next(line for line in self.result.accepted if line.claim_id == "clm-1008")
        codes = [d.code for d in line.diagnoses]
        self.assertEqual(codes, ["J06.9"], "diag_cd_1 and diag_cd_2 are both 'J06.9' and must dedupe to one diagnosis")

    def test_multiple_procedures_preserve_sequence_and_dates(self):
        line = next(line for line in self.result.accepted if line.claim_id == "clm-1007")
        self.assertEqual(len(line.procedures), 2)
        self.assertEqual([p.sequence for p in line.procedures], [1, 2])
        self.assertIsNotNone(line.procedures[0].code_date)

    def test_every_diagnosis_and_procedure_traces_to_an_accepted_claim_line(self):
        accepted_ids = {(line.claim_id, line.line_no) for line in self.result.accepted}
        for line in self.result.accepted:
            for dx in line.diagnoses:
                with self.subTest(claim_id=dx.claim_id, seq=dx.sequence):
                    self.assertIn((dx.claim_id, dx.line_no), accepted_ids)


class TestCodeSystemsAndZeroPadding(_RepresentativeSampleTestCase):
    def test_rev_code_and_bill_type_code_retain_leading_zeros_as_strings(self):
        line = next(line for line in self.result.accepted if line.claim_id == "clm-1006")
        self.assertEqual(line.rev_code, "0001")
        self.assertIsInstance(line.rev_code, str)
        self.assertEqual(line.bill_type_code, "011")
        self.assertIsInstance(line.bill_type_code, str)
        # The whole point: casting to int would silently destroy the value.
        self.assertNotEqual(int(line.rev_code), line.rev_code)

    def test_no_coded_field_is_ever_cast_to_a_numeric_type(self):
        source = (REPO_ROOT / "src" / "tuva_ingest" / "claims_mapping.py").read_text(encoding="utf-8")
        forbidden_casts = (
            'int(row.get("rev_code"',
            'int(row.get("bill_type_code"',
            'int(row.get("place_of_service_code"',
        )
        for forbidden in forbidden_casts:
            self.assertNotIn(forbidden, source)


class TestEligibilityConsolidation(_RepresentativeSampleTestCase):
    def _spans_for(self, person_id):
        matching = (s for s in self.eligibility_result.consolidated if s.person_id == person_id)
        return sorted(matching, key=lambda s: s.start_date)

    def test_overlapping_spans_merge(self):
        spans = self._spans_for("person-001")
        self.assertEqual(len(spans), 1)
        self.assertEqual(str(spans[0].start_date), "2025-01-01")
        self.assertEqual(str(spans[0].end_date), "2025-05-31")

    def test_adjacent_spans_merge(self):
        spans = self._spans_for("person-002")
        self.assertEqual(len(spans), 1)
        self.assertEqual(str(spans[0].start_date), "2025-01-01")
        self.assertEqual(str(spans[0].end_date), "2025-06-30")

    def test_gapped_spans_stay_separate(self):
        spans = self._spans_for("person-003")
        self.assertEqual(len(spans), 2, "a real gap between spans must not be bridged")

    def test_duplicate_spans_collapse(self):
        spans = self._spans_for("person-004")
        self.assertEqual(len(spans), 1, "an exact-duplicate span must collapse to one")

    def test_open_ended_span_is_preserved(self):
        spans = self._spans_for("person-006")
        self.assertEqual(len(spans), 1)
        self.assertIsNone(spans[0].end_date)

    def test_invalid_range_is_quarantined_not_loaded(self):
        self.assertEqual(self._spans_for("person-005"), [])
        reasons = [q.reason for q in self.eligibility_result.quarantined if "span_end_dt" in q.field]
        self.assertTrue(
            any("before" in r for r in reasons),
            "the end-before-start row must be quarantined with a reason naming the field",
        )

    def test_no_overlaps_in_consolidated_output(self):
        overlaps = cm.find_span_overlaps(self.eligibility_result.consolidated)
        self.assertEqual(overlaps, [], f"consolidated eligibility spans still overlap: {overlaps}")


class TestInvalidRecordsAreRejectedOrQuarantined(_RepresentativeSampleTestCase):
    def test_missing_required_fields_are_rejected_with_a_named_field(self):
        fields = {r.field for r in self.result.rejected}
        self.assertIn("clm_id", fields)
        self.assertIn("line_no", fields)

    def test_unresolvable_eligibility_member_is_quarantined(self):
        # eligibility_sample.csv only references member_key values that
        # ARE in the crosswalk, so this proves the quarantine path
        # itself works by feeding it one that is not.
        rows = [{"member_key": "mk-does-not-exist", "payer": "X", "plan": "Y", "coverage_type": "medical",
                 "span_start_dt": "2025-01-01", "span_end_dt": "2025-01-31"}]
        result = cm.consolidate_eligibility(rows, self.crosswalk)
        self.assertEqual(result.consolidated, [])
        self.assertEqual(len(result.quarantined), 1)
        self.assertEqual(result.quarantined[0].field, "member_key")


class TestReadinessGate(_RepresentativeSampleTestCase):
    def test_representative_sample_passes_the_readiness_gate(self):
        readiness = cm.historical_ingestion_ready()
        self.assertTrue(readiness.ready, f"representative sample failed the readiness gate: {readiness.reasons}")
        self.assertEqual(readiness.reasons, [])

    def test_readiness_gate_fails_when_mapping_sheet_is_missing(self):
        readiness = cm.historical_ingestion_ready(mapping_sheet_path=REPO_ROOT / "docs" / "DOES_NOT_EXIST.csv")
        self.assertFalse(readiness.ready)
        self.assertTrue(any("does not exist" in r for r in readiness.reasons))

    def test_readiness_gate_fails_when_a_placeholder_is_present(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bad_doc = Path(tmp) / "bad_decisions.md"
            bad_doc.write_text("This decision is still TBD.\n", encoding="utf-8")
            readiness = cm.historical_ingestion_ready(decisions_doc_path=bad_doc)
            self.assertFalse(readiness.ready)
            self.assertTrue(any("placeholder" in r for r in readiness.reasons))


if __name__ == "__main__":
    unittest.main()
