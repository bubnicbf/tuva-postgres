"""Database-free, network-free static checks that `docs/SOURCE_CONTRACT.md`
-- the source-specific extraction contract for whatever upstream is
configured as `TUVA_API_MANIFEST_URL`/`SOURCE_NAME` (see
`src/tuva_ingest/config.py`) -- exists, covers every required topic, is
free of unresolved placeholders, carries a valid "Last verified" date,
contains no obvious committed secrets, and that any embedded structured
(JSON) examples actually parse.

This is the automated gate required before an extraction implementation
can be considered complete: it never invokes dbt or a database, so it
runs everywhere `make test-unit` runs (unit tests only, no network, no
PostgreSQL). It complements (does not replace) `tests/unit/
test_manifest.py`/`tests/unit/test_api_client.py`, which validate the
wire-format/client code itself, and `tests/unit/
test_input_layer_contract.py`, whose static-check style this module
follows.

Every failure message below names both the missing/invalid section and
the source this document governs (`SOURCE_NAME`, default `"tuva"` --
see `src/tuva_ingest/config.py`), so a broken contract doc fails loudly
and specifically rather than with a generic assertion error.
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.config import IngestConfig
from tuva_ingest.manifest import RAW_TABLES

CONTRACT_PATH = REPO_ROOT / "docs" / "SOURCE_CONTRACT.md"

# The source this contract document governs when no override is set --
# used only to make failure messages specific, not to change what is
# validated (the document must be complete regardless of which source
# name a given deployment configures).
DEFAULT_SOURCE_NAME = "tuva"

# Every topic the task's source-contract requirement enumerates. Each
# must appear as its own numbered "## N. Title" heading in the document,
# in this exact order, so a future edit can't silently drop one without
# renumbering (which this test would also catch).
REQUIRED_TOPIC_HEADINGS = [
    "## 1. Base URL and API version",
    "## 2. Authentication",
    "## 3. Endpoints and expected record grain",
    "## 4. Pagination",
    "## 5. Rate limits and retry behavior",
    "## 6. Incremental extraction field",
    "## 7. Historical mutability",
    "## 8. Corrections, reversals, denials, and deletions",
    "## 9. Maximum expected backfill volume",
    "## 10. Source timezone and timestamp precision",
    "## 11. Identifier stability",
    "## 12. Schema and version-change policy",
    "## 13. PHI classification",
    "## 14. Reconciliation totals",
]

# Metadata sections required alongside the 14 topics: a status/provenance
# legend, a readiness verdict, an owner (or an explicit statement that no
# ownership convention exists), and a "Last verified" date.
REQUIRED_METADATA_HEADINGS = [
    "## Status key",
    "## Readiness",
    "## Owner",
    "## Last verified",
]

# Topics whose correctness/security impact means an "Unverified" fact
# there must keep the document's overall readiness verdict blocked (see
# the task's "unresolved items... cause readiness to remain blocked"
# requirement). Matched against the heading text.
BLOCKING_IF_UNVERIFIED_HEADINGS = [
    "## 2. Authentication",
    "## 4. Pagination",
    "## 8. Corrections, reversals, denials, and deletions",
    "## 13. PHI classification",
    "## 14. Reconciliation totals",
]

PLACEHOLDER_MARKERS = [
    r"\bTODO\b",
    r"\bTBD\b",
    r"\bFIXME\b",
    r"fill this in",
    r"\bXXX\b",
    r"lorem ipsum",
    r"<insert[^>]*>",
    r"\bchange[_ -]?me\b",
]

# Patterns that would indicate a real secret was pasted into the
# document, mirroring the intent of tests/unit/test_input_layer_contract
# .py's TestProfilesExampleHasNoRealCredentials.
SECRET_PATTERNS = [
    r"AKIA[0-9A-Z]{16}",  # AWS access key id shape
    r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----",
    r"gh[pousr]_[A-Za-z0-9]{20,}",  # GitHub token shapes
    r"sk-[A-Za-z0-9]{20,}",  # generic vendor secret-key shape
    r"Bearer\s+[A-Za-z0-9\-_\.]{24,}",  # a literal, non-placeholder bearer token
]


def _read_contract_text() -> str:
    return CONTRACT_PATH.read_text(encoding="utf-8")


def _source_name() -> str:
    # Best-effort: reuse the connector's own default rather than
    # duplicating it, so this test never drifts from config.py's actual
    # default if that default ever changes.
    try:
        return IngestConfig.load(required=frozenset()).source_name
    except Exception:
        return DEFAULT_SOURCE_NAME


def _section_body(text: str, heading: str) -> str:
    """Return the text between `heading` and the next `## `/`# ` heading
    (or end of file). Raises AssertionError with a source-specific
    message if `heading` is not present at all."""
    idx = text.find(heading)
    if idx == -1:
        raise AssertionError(
            f"docs/SOURCE_CONTRACT.md is missing required section {heading!r} "
            f"for source {_source_name()!r} -- extraction cannot be considered "
            "documentation-complete without it"
        )
    rest = text[idx + len(heading) :]
    match = re.search(r"\n#{1,2}\s", rest)
    return rest[: match.start()] if match else rest


class TestSourceContractDocumentExists(unittest.TestCase):
    def test_source_contract_file_exists(self):
        self.assertTrue(
            CONTRACT_PATH.is_file(),
            f"docs/SOURCE_CONTRACT.md must exist before extraction against source "
            f"{_source_name()!r} can be considered complete (see task requirement: "
            "source-contract documentation must precede extraction code).",
        )

    def test_source_contract_is_nonempty(self):
        text = _read_contract_text()
        self.assertGreater(
            len(text.strip()), 500,
            f"docs/SOURCE_CONTRACT.md exists but is too short to be a real contract "
            f"for source {_source_name()!r}",
        )


class TestRequiredTopicsPresentAndNonEmpty(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = _read_contract_text()

    def test_every_required_topic_heading_is_present(self):
        for heading in REQUIRED_TOPIC_HEADINGS:
            with self.subTest(heading=heading):
                self.assertIn(
                    heading, self.text,
                    f"docs/SOURCE_CONTRACT.md is missing required topic {heading!r} "
                    f"for source {_source_name()!r}",
                )

    def test_every_required_metadata_heading_is_present(self):
        for heading in REQUIRED_METADATA_HEADINGS:
            with self.subTest(heading=heading):
                self.assertIn(
                    heading, self.text,
                    f"docs/SOURCE_CONTRACT.md is missing required section {heading!r} "
                    f"for source {_source_name()!r}",
                )

    def test_headings_appear_in_numeric_order(self):
        positions = [self.text.index(h) for h in REQUIRED_TOPIC_HEADINGS]
        self.assertEqual(
            positions, sorted(positions),
            "docs/SOURCE_CONTRACT.md's numbered topic headings are out of order "
            "(a heading may have been dropped, duplicated, or renumbered incorrectly)",
        )

    def test_every_topic_section_has_substantive_non_empty_content(self):
        for heading in REQUIRED_TOPIC_HEADINGS:
            with self.subTest(heading=heading):
                body = _section_body(self.text, heading).strip()
                self.assertGreater(
                    len(body), 120,
                    f"docs/SOURCE_CONTRACT.md's {heading!r} section is empty or too "
                    f"thin to be an actionable fact for source {_source_name()!r}",
                )

    def test_every_topic_section_carries_an_explicit_provenance_tag(self):
        # Every topic must be traceable to one of the document's own
        # status tags (see "## Status key") -- a section that asserts
        # facts with no Verified/Unverified/Decision/assumption
        # provenance is exactly the kind of unactionable checklist filler
        # this task's documentation-quality requirements rule out.
        tag_pattern = re.compile(
            r"\*\*(Verified|Unverified|Decision|Repository-derived assumption)\b",
        )
        for heading in REQUIRED_TOPIC_HEADINGS:
            with self.subTest(heading=heading):
                body = _section_body(self.text, heading)
                self.assertTrue(
                    tag_pattern.search(body),
                    f"docs/SOURCE_CONTRACT.md's {heading!r} section has no explicit "
                    "Verified/Unverified/Decision/assumption provenance tag for "
                    f"source {_source_name()!r}",
                )


class TestNoUnresolvedPlaceholders(unittest.TestCase):
    def test_document_contains_no_placeholder_markers(self):
        text = _read_contract_text()
        for pattern in PLACEHOLDER_MARKERS:
            with self.subTest(pattern=pattern):
                match = re.search(pattern, text, re.IGNORECASE)
                self.assertIsNone(
                    match,
                    f"docs/SOURCE_CONTRACT.md contains an unresolved placeholder "
                    f"({match.group(0) if match else pattern!r}) -- source "
                    f"{_source_name()!r} cannot be considered extraction-ready "
                    "while placeholders remain",
                )


class TestUnverifiedItemsKeepReadinessBlocked(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = _read_contract_text()
        cls.readiness = _section_body(cls.text, "## Readiness")

    def test_correctness_or_security_critical_sections_contain_unverified_markers(self):
        # Sanity check on the fixture itself: confirm the topics this
        # task calls out as correctness/security-critical actually do
        # carry at least one Unverified item today (i.e. this repository
        # has not silently connected a real, fully-confirmed vendor
        # without updating the document).
        for heading in BLOCKING_IF_UNVERIFIED_HEADINGS:
            with self.subTest(heading=heading):
                body = _section_body(self.text, heading)
                self.assertIn("Unverified", body)

    def test_readiness_section_states_extraction_is_blocked_for_a_real_vendor(self):
        self.assertIn(
            "blocked", self.readiness.lower(),
            "docs/SOURCE_CONTRACT.md's Readiness section must explicitly say "
            f"extraction is blocked for source {_source_name()!r} while "
            "correctness/security-critical facts (pagination completeness, "
            "incremental correctness, deletion handling, PHI safety, "
            "reconciliation) remain Unverified",
        )

    def test_readiness_section_does_not_unconditionally_claim_full_readiness(self):
        # "ready" is allowed to appear (e.g. "may proceed... is already
        # built and tested"), but the section must not claim the source is
        # unconditionally ready without also stating the blocking
        # conditions -- i.e. "blocked" must co-occur with any readiness
        # claim.
        self.assertNotRegex(
            self.readiness,
            r"is (fully |completely )?ready(?!.{0,80}blocked)",
            "docs/SOURCE_CONTRACT.md's Readiness section must not claim "
            f"unconditional readiness for source {_source_name()!r} while "
            "blocking items remain Unverified",
        )


class TestLastVerifiedDate(unittest.TestCase):
    def test_last_verified_section_has_a_valid_iso_date(self):
        text = _read_contract_text()
        body = _section_body(text, "## Last verified")
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", body)
        self.assertIsNotNone(
            match,
            f"docs/SOURCE_CONTRACT.md's 'Last verified' section has no "
            f"YYYY-MM-DD date for source {_source_name()!r}",
        )
        try:
            verified_date = date.fromisoformat(match.group(0))
        except ValueError:
            self.fail(f"docs/SOURCE_CONTRACT.md's 'Last verified' date {match.group(0)!r} is not a valid calendar date")
        self.assertLessEqual(
            verified_date, date.today(),
            f"docs/SOURCE_CONTRACT.md's 'Last verified' date {verified_date} is in the future",
        )


class TestNoCommittedSecrets(unittest.TestCase):
    def test_document_contains_no_secret_shaped_strings(self):
        text = _read_contract_text()
        for pattern in SECRET_PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertNotRegex(
                    text, pattern,
                    f"docs/SOURCE_CONTRACT.md appears to contain a real credential "
                    f"matching {pattern!r} -- only redacted placeholders are allowed",
                )

    def test_example_env_assignments_are_redacted_placeholders_only(self):
        text = _read_contract_text()
        for match in re.finditer(r'export (TUVA_API_TOKEN|PG_DSN)="([^"]*)"', text):
            value = match.group(2)
            with self.subTest(var=match.group(1)):
                self.assertTrue(
                    value == "" or "redacted" in value.lower() or "example" in value.lower(),
                    f"docs/SOURCE_CONTRACT.md's example {match.group(1)}={value!r} must be "
                    "empty or an explicitly redacted/example placeholder, never a real secret",
                )


class TestEmbeddedStructuredExamplesParse(unittest.TestCase):
    def test_every_fenced_json_block_parses_as_valid_json(self):
        text = _read_contract_text()
        blocks = re.findall(r"```json\n(.*?)```", text, re.DOTALL)
        self.assertGreater(
            len(blocks), 0,
            "docs/SOURCE_CONTRACT.md should include at least one worked JSON example "
            "of the manifest shape it describes",
        )
        for i, block in enumerate(blocks):
            with self.subTest(block_index=i):
                try:
                    json.loads(block)
                except json.JSONDecodeError as exc:
                    self.fail(f"docs/SOURCE_CONTRACT.md's JSON example #{i} does not parse: {exc}")


class TestSourceLinkedToDocumentedTables(unittest.TestCase):
    """The closest thing this repository has to an 'extraction
    registration mechanism' is `manifest.RAW_TABLES` (the fixed set of
    raw tables the connector extracts, loads, and maps into the Tuva
    Input Layer -- see manifest.py's module docstring and
    models/sources.yml). Every managed table must be named in the source
    contract so the document and the code can't silently drift apart."""

    def test_every_managed_raw_table_is_named_in_the_source_contract(self):
        text = _read_contract_text()
        for table in RAW_TABLES:
            with self.subTest(table=table):
                self.assertIn(
                    table, text,
                    f"docs/SOURCE_CONTRACT.md never mentions managed raw table "
                    f"{table!r} (see manifest.RAW_TABLES) -- the contract must cover "
                    f"every table source {_source_name()!r} extracts",
                )

    def test_contract_references_its_implementation_modules(self):
        text = _read_contract_text()
        for module in ("api_client.py", "manifest.py", "extract.py", "raw_loader.py"):
            with self.subTest(module=module):
                self.assertIn(module, text)


if __name__ == "__main__":
    unittest.main()
