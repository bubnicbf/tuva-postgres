"""Database-free, network-free static checks that this connector's dbt
project actually conforms to the Tuva 0.18.0 Input Layer contract at
the file level: package pin, project flags/domain vars, required final
models, no select-star, typed-NULL usage, source()/ref() discipline,
input_layer tagging, and structural-DQ-gates-later-DQ ordering.

These tests parse packages.yml/dbt_project.yml/Makefile/CI YAML and the
SQL model text directly with the standard library + PyYAML -- they do
NOT invoke dbt, and do NOT require a database or network access, so
they run everywhere `make test-unit` runs. They are a *complement* to
(not a replacement for) the real `dbt build --select tag:input_layer` /
`tag:dq_structural` runs and the information_schema-introspecting
assertions in tests/integration/test_pipeline_integration.py, which
require a real Postgres + a real pinned-package fetch and therefore can
only prove the contract holds in an environment with both.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))


def _load_yaml(relative_path: str):
    with open(REPO_ROOT / relative_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class TestPackagesPin(unittest.TestCase):
    def test_the_tuva_project_is_pinned_to_exactly_0_18_0(self):
        packages = _load_yaml("packages.yml")["packages"]
        tuva = [p for p in packages if p.get("package") == "tuva-health/the_tuva_project"]
        self.assertEqual(len(tuva), 1, "packages.yml must declare tuva-health/the_tuva_project exactly once")
        version = tuva[0].get("version")
        self.assertEqual(
            str(version), "0.18.0",
            f"tuva-health/the_tuva_project must be pinned to exactly '0.18.0', not a range/main/latest/git revision (got {version!r})",
        )
        self.assertNotIn("git", tuva[0], "the Tuva package must come from dbt Hub (package:), not an unpinned git revision")

    def test_no_package_uses_a_floating_git_revision_without_a_pinned_tag(self):
        packages = _load_yaml("packages.yml")["packages"]
        for pkg in packages:
            if "git" in pkg:
                self.assertIn("revision", pkg, f"git-sourced package {pkg['git']} must pin an exact revision/tag")
                self.assertNotIn(pkg.get("revision"), {"main", "master", "latest", "HEAD"})


class TestDbtProjectDomainConfiguration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = _load_yaml("dbt_project.yml")

    def test_required_ref_search_flag_is_set(self):
        self.assertTrue(self.project.get("flags", {}).get("require_ref_searches_node_package_before_root") is True)

    def test_claims_enabled(self):
        self.assertIs(self.project["vars"]["claims_enabled"], True)

    def test_unimplemented_domains_stay_disabled(self):
        # This connector only implements the claims Input Layer models
        # (eligibility, medical_claim, pharmacy_claim). Per the Input
        # Layer contract, a domain must never be enabled while only
        # part of its interface is implemented -- see README.md "How to
        # add another Tuva domain safely".
        for var in ("clinical_enabled", "provider_attribution_enabled", "semantic_layer_enabled"):
            with self.subTest(var=var):
                self.assertIs(self.project["vars"].get(var), False, f"{var} must stay false until this connector implements that domain's full Input Layer interface")

    def test_final_and_staging_models_are_tagged_input_layer(self):
        models_cfg = self.project["models"]["tuva_ingest_connector"]
        for layer in ("staging", "final"):
            with self.subTest(layer=layer):
                self.assertIn("input_layer", models_cfg[layer].get("+tags", []))

    def test_final_models_materialize_as_tables(self):
        self.assertEqual(self.project["models"]["tuva_ingest_connector"]["final"]["+materialized"], "table")


REQUIRED_CLAIMS_FINAL_MODELS = {"eligibility", "medical_claim", "pharmacy_claim"}

ELIGIBILITY_CONTRACT_COLUMNS = [
    "person_id", "member_id", "subscriber_id", "gender", "race", "birth_date", "death_date",
    "death_flag", "enrollment_start_date", "enrollment_end_date", "payer", "payer_type", "plan",
    "original_reason_entitlement_code", "dual_status_code", "medicare_status_code", "group_id",
    "group_name", "name_suffix", "first_name", "middle_name", "last_name", "email", "ethnicity",
    "social_security_number", "subscriber_relation", "address", "city", "state", "zip_code",
    "phone", "data_source", "file_name", "file_date", "ingest_datetime",
]

PHARMACY_CLAIM_CONTRACT_COLUMNS = [
    "claim_id", "claim_line_number", "person_id", "member_id", "payer", "plan",
    "prescribing_provider_npi", "dispensing_provider_npi", "dispensing_date", "ndc_code",
    "quantity", "days_supply", "refills", "paid_date", "paid_amount", "allowed_amount",
    "charge_amount", "coinsurance_amount", "copayment_amount", "deductible_amount",
    "in_network_flag", "data_source", "file_name", "file_date", "ingest_datetime",
]

MEDICAL_CLAIM_CONTRACT_COLUMNS = (
    [
        "claim_id", "claim_line_number", "claim_type", "person_id", "member_id", "payer", "plan",
        "claim_start_date", "claim_end_date", "claim_line_start_date", "claim_line_end_date",
        "admission_date", "discharge_date", "paid_date", "admit_source_code", "admit_type_code",
        "discharge_disposition_code", "place_of_service_code", "bill_type_code",
        "revenue_center_code", "drg_code_type", "drg_code", "service_unit_quantity", "hcpcs_code",
        "hcpcs_modifier_1", "hcpcs_modifier_2", "hcpcs_modifier_3", "hcpcs_modifier_4",
        "hcpcs_modifier_5", "rendering_npi", "rendering_tin", "billing_npi", "billing_tin",
        "facility_npi", "paid_amount", "allowed_amount", "charge_amount", "coinsurance_amount",
        "copayment_amount", "deductible_amount", "total_cost_amount", "diagnosis_code_type",
    ]
    + [f"diagnosis_code_{i}" for i in range(1, 26)]
    + [f"diagnosis_poa_{i}" for i in range(1, 26)]
    + ["procedure_code_type"]
    + [f"procedure_code_{i}" for i in range(1, 26)]
    + [f"procedure_date_{i}" for i in range(1, 26)]
    + ["in_network_flag", "data_source", "file_name", "file_date", "ingest_datetime"]
)


def _read_model(name: str) -> str:
    return (REPO_ROOT / "models" / "final" / f"{name}.sql").read_text(encoding="utf-8")


class TestFinalModelsExistAndAreComplete(unittest.TestCase):
    def test_all_three_required_claims_final_models_exist(self):
        final_dir = REPO_ROOT / "models" / "final"
        present = {p.stem for p in final_dir.glob("*.sql")}
        self.assertEqual(present, REQUIRED_CLAIMS_FINAL_MODELS)

    def test_no_final_model_uses_select_star(self):
        for name in REQUIRED_CLAIMS_FINAL_MODELS:
            with self.subTest(model=name):
                sql = _read_model(name)
                # Match "select *" but not "select * from final" is still
                # a select-star, so check literally for any bare star
                # after select/from (excluding "select * from staging"/
                # "from final" wrapper CTEs is NOT allowed either --
                # every column must be named explicitly per the contract).
                self.assertIsNone(
                    re.search(r"select\s+\*\s+from\s+(staging|source)\b", sql, re.IGNORECASE),
                    f"{name}.sql must not select * from its staging/source CTE -- every Input Layer column must be named explicitly",
                )

    def test_final_models_use_ref_not_hardcoded_schema(self):
        for name in REQUIRED_CLAIMS_FINAL_MODELS:
            with self.subTest(model=name):
                sql = _read_model(name)
                self.assertIn("{{ ref(", sql)
                self.assertNotRegex(sql, r"from\s+\"?[a-z_]+\"?\.\"?stg_", "final models must ref() staging models, never reference a hard-coded schema-qualified table name")

    def test_final_models_are_tagged_input_layer(self):
        for name in REQUIRED_CLAIMS_FINAL_MODELS:
            with self.subTest(model=name):
                sql = _read_model(name)
                self.assertIn("input_layer", sql, f"{name}.sql must declare tags=['input_layer'] in its config()")

    def _assert_all_columns_present_and_explicitly_cast(self, name: str, expected_columns: list[str]):
        sql = _read_model(name)
        # Collect every literal "... as <column_name>" alias in the final
        # select list (case-insensitive `as`), which is how every
        # non-loop-generated column in these models -- real or
        # typed-NULL -- is produced.
        aliases = re.findall(r"\bas\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*,?\s*$", sql, re.IGNORECASE | re.MULTILINE)
        # medical_claim.sql generates its 100 numbered diagnosis/procedure
        # columns via a Jinja `{%- for i in range(1, 26) %}` loop (see
        # that model's header comment) rather than 100 literal lines, so
        # the raw (uncompiled) template text has aliases like
        # `diagnosis_code_{{ i }}`, not `diagnosis_code_1`. Detect those
        # loop-generated prefixes and expand them the same way dbt's
        # Jinja renderer would.
        for prefix in re.findall(r"\bas\s+([a-zA-Z_][a-zA-Z0-9_]*)_\{\{\s*i\s*\}\}", sql, re.IGNORECASE):
            aliases.extend(f"{prefix}_{i}" for i in range(1, 26))
        missing = [c for c in expected_columns if c not in aliases]
        self.assertEqual(missing, [], f"{name}.sql is missing contract columns: {missing}")
        extra = [c for c in aliases if c not in expected_columns]
        self.assertEqual(extra, [], f"{name}.sql produces columns outside the Input Layer contract: {extra}")

    def test_eligibility_exposes_the_full_contract(self):
        self._assert_all_columns_present_and_explicitly_cast("eligibility", ELIGIBILITY_CONTRACT_COLUMNS)

    def test_medical_claim_exposes_the_full_contract(self):
        self._assert_all_columns_present_and_explicitly_cast("medical_claim", MEDICAL_CLAIM_CONTRACT_COLUMNS)

    def test_pharmacy_claim_exposes_the_full_contract(self):
        self._assert_all_columns_present_and_explicitly_cast("pharmacy_claim", PHARMACY_CLAIM_CONTRACT_COLUMNS)

    def test_typed_nulls_are_never_bare(self):
        # Every synthetic NULL in these models must be an explicit
        # `cast(null as <type>)`, never a bare, untyped `null`. Legitimate
        # SQL predicates (`is null` / `is not null`, e.g. deriving
        # death_flag) are not synthetic contract NULLs and are exempted.
        for name in REQUIRED_CLAIMS_FINAL_MODELS:
            with self.subTest(model=name):
                sql = _read_model(name)
                stripped = re.sub(r"cast\(\s*null\s+as\s+[a-zA-Z0-9_(), ]+\)", "", sql, flags=re.IGNORECASE)
                stripped = re.sub(r"\bis\s+not\s+null\b", "", stripped, flags=re.IGNORECASE)
                stripped = re.sub(r"\bis\s+null\b", "", stripped, flags=re.IGNORECASE)
                stripped_no_comments = re.sub(r"--[^\n]*", "", stripped)
                self.assertNotRegex(
                    stripped_no_comments, r"\bnull\b", f"{name}.sql has a bare, untyped NULL outside of cast(null as ...)/is (not) null"
                )


class TestSchemaYamlCoversFinalModels(unittest.TestCase):
    def test_final_schema_yml_declares_every_required_model_with_input_layer_tag_and_pk_test(self):
        schema = _load_yaml("models/final/schema.yml")
        declared = {m["name"]: m for m in schema["models"]}
        self.assertEqual(set(declared), REQUIRED_CLAIMS_FINAL_MODELS)
        for name, model in declared.items():
            with self.subTest(model=name):
                self.assertIn("input_layer", model.get("config", {}).get("tags", []))
                combo_tests = [
                    t["dbt_utils.unique_combination_of_columns"]
                    for t in model.get("data_tests", [])
                    if "dbt_utils.unique_combination_of_columns" in t
                ]
                self.assertEqual(len(combo_tests), 1, f"{name} must declare exactly one composite primary-key uniqueness test")
                self.assertIn("data_source", combo_tests[0]["combination_of_columns"], f"{name}'s primary-key test must include data_source (multi-source collision protection)")


class TestNoDirectLoadIntoTuvaManagedSchemas(unittest.TestCase):
    """Structural DQ / source-loader safety: nothing in this repository's
    own code writes directly into a Tuva-managed schema name (core,
    terminology, data_marts, dq -- see README.md 'This repository does
    not own Tuva's DDL')."""

    _TUVA_MANAGED_SCHEMA_TOKENS = ("core", "terminology", "data_marts", "dq")

    def test_raw_loader_only_targets_configured_raw_schema(self):
        raw_loader_src = (REPO_ROOT / "src" / "tuva_ingest" / "raw_loader.py").read_text(encoding="utf-8")
        for token in self._TUVA_MANAGED_SCHEMA_TOKENS:
            self.assertNotIn(f'"{token}"', raw_loader_src)
            self.assertNotIn(f"'{token}'", raw_loader_src)

    def test_migrations_never_reference_tuva_managed_schema_names(self):
        migrations_dir = REPO_ROOT / "migrations"
        for sql_file in migrations_dir.rglob("*.sql"):
            text = sql_file.read_text(encoding="utf-8").lower()
            for token in self._TUVA_MANAGED_SCHEMA_TOKENS:
                self.assertNotIn(
                    f"schema {token}", text,
                    f"{sql_file.relative_to(REPO_ROOT)} must never create/reference a Tuva-managed '{token}' schema",
                )


class TestProfilesExampleHasNoRealCredentials(unittest.TestCase):
    def test_profiles_example_contains_only_env_var_driven_placeholders(self):
        text = (REPO_ROOT / "profiles.example.yml").read_text(encoding="utf-8")
        self.assertNotIn("BEGIN PRIVATE KEY", text)
        # Every password/user value must be `{{ env_var(...) }}`-driven,
        # never a bare literal secret string.
        for match in re.finditer(r"(password|user)\s*:\s*(.+)", text):
            value = match.group(2).strip()
            self.assertTrue(
                value.startswith('"{{ env_var(') or value.startswith("{{ env_var("),
                f"profiles.example.yml's {match.group(1)} must be env_var()-driven, found: {value!r}",
            )


class TestValidationOrdering(unittest.TestCase):
    """Confirms the Makefile and CLI actually invoke structural DQ before
    any logical/analytical DQ step, by inspecting the source text/line
    order directly -- no dbt/database required."""

    def test_makefile_pipeline_target_runs_input_layer_before_dq_structural(self):
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        pipeline_recipe = makefile.split("pipeline: quality", 1)[1].split("\n\n", 1)[0]
        input_layer_idx = pipeline_recipe.index("dbt-input-layer")
        dq_structural_idx = pipeline_recipe.index("dbt-dq-structural")
        self.assertLess(input_layer_idx, dq_structural_idx, "make pipeline must build tag:input_layer before tag:dq_structural")

    def test_makefile_declares_dq_structural_before_dq_logical_and_analytical_targets(self):
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        structural_idx = makefile.index("dbt-dq-structural:")
        logical_idx = makefile.index("dbt-dq-logical:")
        analytical_idx = makefile.index("dbt-dq-analytical:")
        self.assertLess(structural_idx, logical_idx)
        self.assertLess(logical_idx, analytical_idx)

    def test_cli_run_command_builds_input_layer_before_dq_structural(self):
        cli_src = (REPO_ROOT / "src" / "tuva_ingest" / "cli.py").read_text(encoding="utf-8")
        input_layer_idx = cli_src.index('"tag:input_layer"')
        dq_structural_idx = cli_src.index('"tag:dq_structural"')
        self.assertLess(input_layer_idx, dq_structural_idx, "_cmd_run must build tag:input_layer before tag:dq_structural")

    def test_ci_workflow_runs_input_layer_before_dq_structural_before_integration_suite(self):
        ci_src = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        # Anchor on the actual step declarations (`- name: ...`), not just
        # any mention of the string anywhere (e.g. in a forward-referencing
        # comment), so this test reflects real step execution order.
        input_layer_idx = ci_src.index("- name: dbt build --select tag:input_layer")
        dq_structural_idx = ci_src.index("- name: dbt build --select tag:dq_structural")
        integration_suite_idx = ci_src.index("- name: Run the full disposable-database integration suite")
        self.assertLess(input_layer_idx, dq_structural_idx)
        self.assertLess(dq_structural_idx, integration_suite_idx)


if __name__ == "__main__":
    unittest.main()
