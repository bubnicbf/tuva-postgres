"""Database-free, network-free static checks that this connector's dbt
project actually keeps the raw -> staging -> intermediate -> final layer
boundaries this repository's architecture requires (see README.md
"Architecture" and docs/CLAIMS_MAPPING_DECISIONS.md).

These tests parse dbt_project.yml/model SQL/schema YAML text directly
with the standard library + PyYAML -- they do NOT invoke dbt and do NOT
require a database or network access, so they run everywhere
`make test-unit` runs. They complement (never replace)
tests/unit/test_input_layer_contract.py (the Input Layer contract
itself) and the real `dbt build --select tag:input_layer` /
information_schema-introspecting assertions in
tests/integration/test_pipeline_integration.py, which require a real
Postgres + a real pinned-package fetch.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

STAGING_DIR = REPO_ROOT / "models" / "staging"
INTERMEDIATE_DIR = REPO_ROOT / "models" / "intermediate"
FINAL_DIR = REPO_ROOT / "models" / "final"

REQUIRED_INTERMEDIATE_MODELS = {
    "int_member_crosswalk",
    "int_eligibility_resolved",
    "int_eligibility_spans",
    "int_medical_claim_lines",
    "int_pharmacy_claim_lines",
}


def _load_yaml(relative_path: str):
    with open(REPO_ROOT / relative_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


_SQL_LINE_COMMENT = re.compile(r"--[^\n]*")


def _read_without_comments(path: Path) -> str:
    """Strip `-- ...` line comments before substring/regex checks below,
    so a comment that documents/mentions a Jinja call as prose (e.g.
    explaining what a future source() migration would look like) can
    never be mistaken for a real, live reference."""
    return _SQL_LINE_COMMENT.sub("", _read(path))


class TestIntermediateModelsExist(unittest.TestCase):
    def test_all_required_intermediate_models_exist(self):
        self.assertTrue(INTERMEDIATE_DIR.is_dir(), "models/intermediate/ must exist")
        present = {p.stem for p in INTERMEDIATE_DIR.glob("*.sql")}
        self.assertEqual(present, REQUIRED_INTERMEDIATE_MODELS)

    def test_intermediate_has_schema_yml_covering_every_model(self):
        schema = _load_yaml("models/intermediate/schema.yml")
        declared = {m["name"] for m in schema["models"]}
        self.assertEqual(declared, REQUIRED_INTERMEDIATE_MODELS)
        for model in schema["models"]:
            with self.subTest(model=model["name"]):
                self.assertIn("input_layer", model.get("config", {}).get("tags", []))


class TestDbtProjectConfiguresIntermediateLayer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = _load_yaml("dbt_project.yml")

    def test_intermediate_layer_is_configured(self):
        models_cfg = self.project["models"]["tuva_ingest_connector"]
        self.assertIn("intermediate", models_cfg)
        intermediate_cfg = models_cfg["intermediate"]
        self.assertIn("input_layer", intermediate_cfg.get("+tags", []))

    def test_intermediate_uses_the_configurable_staging_schema_var(self):
        # Per this task's own guidance: prefer the existing configurable
        # staging schema over a bespoke dedicated physical schema unless
        # the architecture clearly requires one (it does not here -- see
        # dbt_project.yml's own comment on this block).
        intermediate_cfg = self.project["models"]["tuva_ingest_connector"]["intermediate"]
        self.assertEqual(intermediate_cfg.get("+schema"), "{{ var('staging_schema') }}")

    def test_member_crosswalk_seed_is_configured_and_tagged_input_layer(self):
        seeds_cfg = self.project.get("seeds", {}).get("tuva_ingest_connector", {})
        self.assertIn("member_crosswalk_seed", seeds_cfg)
        self.assertIn("input_layer", seeds_cfg["member_crosswalk_seed"].get("+tags", []))


class TestLayerBoundaries(unittest.TestCase):
    """Structural proof of the layer-separation rules this task
    requires: staging sources only raw, intermediate refs
    staging/intermediate (never raw directly), final refs intermediate
    (never staging or raw directly)."""

    def test_staging_models_only_source_raw_never_ref_intermediate_or_final(self):
        for path in STAGING_DIR.glob("*.sql"):
            with self.subTest(model=path.stem):
                sql = _read(path)
                self.assertIn("{{ source(", sql, f"{path.name} must read from a declared source()")
                self.assertNotRegex(
                    sql, r"\{\{\s*ref\(\s*['\"]int_", f"{path.name} must never ref() an intermediate model"
                )
                self.assertNotRegex(
                    sql,
                    r"\{\{\s*ref\(\s*['\"](eligibility|medical_claim|pharmacy_claim)['\"]",
                    f"{path.name} must never ref() a final model",
                )

    def test_intermediate_models_never_source_raw_directly(self):
        for path in INTERMEDIATE_DIR.glob("*.sql"):
            with self.subTest(model=path.stem):
                sql = _read_without_comments(path)
                self.assertNotIn(
                    "{{ source(",
                    sql,
                    f"{path.name} must ref() staging/intermediate/seed models, never source() raw directly (see README.md 'Architecture')",
                )

    def test_final_models_ref_intermediate_never_staging_or_raw(self):
        for name in ("eligibility", "medical_claim", "pharmacy_claim"):
            path = FINAL_DIR / f"{name}.sql"
            with self.subTest(model=name):
                sql = _read(path)
                self.assertNotIn("{{ source(", sql, f"{name}.sql must never source() raw directly")
                self.assertNotRegex(
                    sql,
                    r"\{\{\s*ref\(\s*['\"]stg_",
                    f"{name}.sql must ref() an intermediate model, never a staging model directly",
                )
                self.assertRegex(
                    sql, r"\{\{\s*ref\(\s*['\"]int_", f"{name}.sql must ref() its corresponding intermediate model"
                )

    def test_final_medical_claim_excludes_superseded_and_unmatched_rows(self):
        sql = _read(FINAL_DIR / "medical_claim.sql")
        self.assertIn("matched_member", sql)
        self.assertIn("is_superseded", sql)

    def test_intermediate_models_contain_no_hardcoded_schema_name(self):
        # No deployment schema name literal (raw_incoming/staging_incoming/
        # input_layer/etc.) anywhere in models/intermediate/*.sql -- every
        # relation reference must go through ref()/source(), matching the
        # same rule this repository already enforces for final models.
        hardcoded_schema_pattern = re.compile(
            r"from\s+\"?[a-z_]+\"?\.\"?(int_|stg_|eligibility|medical_claim|pharmacy_claim)"
        )
        for path in INTERMEDIATE_DIR.glob("*.sql"):
            with self.subTest(model=path.stem):
                sql = _read(path)
                self.assertIsNone(
                    hardcoded_schema_pattern.search(sql),
                    f"{path.name} must reference other models via ref(), never a hard-coded schema-qualified name",
                )


class TestReusableMacrosExist(unittest.TestCase):
    def test_claim_type_macro_exists_and_is_used(self):
        macro_path = REPO_ROOT / "macros" / "claim_type.sql"
        self.assertTrue(macro_path.is_file())
        macro_sql = _read(macro_path)
        self.assertIn("macro derive_claim_type", macro_sql)
        lines_sql = _read(INTERMEDIATE_DIR / "int_medical_claim_lines.sql")
        self.assertIn("derive_claim_type(", lines_sql)

    def test_cents_to_amount_macro_exists_and_is_used(self):
        safe_cast_sql = _read(REPO_ROOT / "macros" / "safe_cast.sql")
        self.assertIn("macro cents_to_amount", safe_cast_sql)
        staging_sql = _read(STAGING_DIR / "stg_medical_claim.sql")
        self.assertIn("cents_to_amount(", staging_sql)


class TestMemberCrosswalkSeed(unittest.TestCase):
    def test_seed_file_exists_with_expected_header(self):
        seed_path = REPO_ROOT / "seeds" / "member_crosswalk_seed.csv"
        self.assertTrue(seed_path.is_file())
        lines = _read(seed_path).strip().splitlines()
        self.assertEqual(lines[0], "member_key,person_id")
        self.assertGreater(len(lines), 1, "seed must contain at least one crosswalk row")

    def test_seed_demonstrates_a_many_to_one_merge(self):
        # Same many-to-one identifier-merge scenario documented in docs/
        # CLAIMS_MAPPING_DECISIONS.md decision 5 (mk-002/mk-002b -> person-002).
        seed_path = REPO_ROOT / "seeds" / "member_crosswalk_seed.csv"
        rows = [line.split(",") for line in _read(seed_path).strip().splitlines()[1:]]
        person_ids = [r[1] for r in rows]
        member_keys = [r[0] for r in rows]
        self.assertEqual(len(member_keys), len(set(member_keys)), "member_key must be unique in the crosswalk")
        self.assertLess(len(set(person_ids)), len(person_ids), "seed must demonstrate at least one many-to-one merge")


REQUIRED_SINGULAR_TESTS = {
    "assert_no_overlapping_eligibility_spans.sql",
    "assert_unmatched_eligibility_is_always_quarantined.sql",
    "assert_no_medical_claim_grain_conflicts.sql",
    "assert_orig_clm_id_references_exist.sql",
    "assert_medical_claim_financial_reconciliation.sql",
    "assert_superseded_originals_excluded_from_final_medical_claim.sql",
    "assert_void_nets_to_zero_with_original.sql",
}


class TestSingularDataQualityTestsExist(unittest.TestCase):
    def test_required_singular_tests_are_present(self):
        tests_dir = REPO_ROOT / "tests" / "dbt"
        present = {p.name for p in tests_dir.glob("*.sql")}
        missing = REQUIRED_SINGULAR_TESTS - present
        self.assertEqual(missing, set(), f"tests/dbt/ is missing required singular tests: {missing}")

    def test_singular_tests_ref_intermediate_or_final_models_not_raw(self):
        tests_dir = REPO_ROOT / "tests" / "dbt"
        for name in REQUIRED_SINGULAR_TESTS:
            with self.subTest(test=name):
                sql = _read(tests_dir / name)
                self.assertNotIn("{{ source(", sql)
                self.assertRegex(sql, r"\{\{\s*ref\(")


if __name__ == "__main__":
    unittest.main()
