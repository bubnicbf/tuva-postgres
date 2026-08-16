# Source-to-Tuva claims mapping: required decisions

This document resolves the claims-modeling decisions that
`docs/CLAIMS_MAPPING.csv` depends on, for the incoming claims source
this repository is being extended to onboard (see this project's
`SOURCE_NAME`/`TUVA_API_MANIFEST_URL` configuration and `docs/
SOURCE_CONTRACT.md`, which already established that no concrete vendor
is connected yet). It complements, and does not replace, `docs/
SOURCE_CONTRACT.md` (the API/operational contract) and `docs/
API_MANIFEST.md` (the wire format); this document is scoped to
field-level claims semantics only.

Implementation: `src/tuva_ingest/claims_mapping.py`. Representative
sample: `tests/fixtures/claims_mapping_sample/{claims_sample.csv,
eligibility_sample.csv, member_crosswalk.csv}`. Validated by:
`tests/unit/test_claims_mapping.py`.

## Status key

Same convention as `docs/SOURCE_CONTRACT.md`:

| Tag | Meaning |
| --- | --- |
| **Verified** | Confirmed directly from this repository's code/tests/migrations (citation given). |
| **Repository-derived assumption** | A reasonable inference from repository structure that still needs vendor confirmation. |
| **Unverified** | Cannot be confirmed from this repository or an authoritative, currently connected vendor source. |
| **Decision** | A choice this implementation makes, independent of vendor confirmation. |

## Why this mapping targets field names the current test fixtures don't use

**Verified:** `tests/fixtures/{eligibility,medical_claim,pharmacy_claim}.csv`
(the connector's existing "tuva" test source) already use Tuva Input
Layer column names directly (`claim_id`, `person_id`, `paid_amount`,
...) -- see `models/staging/stg_medical_claim.sql`'s header: "Column
names here match the Tuva Input Layer `medical_claim` contract directly
... so `models/final/medical_claim.sql`'s mapping is a plain
select/cast, not a rename." That existing source has no vendor-style
abbreviated fields (`clm_id`, `line_no`, `member_key`, `paid_cents`) and
no member-identity crosswalk to build, because it was built as a
Tuva-shaped test double, not a real vendor extract.

**Decision:** this mapping instead documents the field-abbreviated,
cents-denominated, vendor-shaped extract format the four mandated rows
imply -- the shape a real incoming claims vendor is likely to deliver --
and implements it end-to-end (crosswalk, unit conversion, claim typing,
adjustment netting, diagnosis/procedure normalization, eligibility
consolidation) against a synthetic representative sample. It is
additive: it does not modify `models/staging/stg_medical_claim.sql`,
`models/staging/stg_eligibility.sql`, or the existing "tuva" test
source's raw shape, and it does not touch `raw`/`ingest_ops` or
`RAW_DATA_DIR`. See "Readiness" below for exactly what this does and
does not unblock.

## 1. Final claim-line grain

- **Verified, already established by this repository:** the final
  claim-line grain is one row per `(claim_id, claim_line_number,
  data_source)`, enforced today by `models/final/schema.yml`'s
  `dbt_utils.unique_combination_of_columns` test on `medical_claim`
  (and identically on `pharmacy_claim`). This mapping adopts the same
  grain for the incoming source rather than inventing a new one.
- `claim_id` + `claim_line_number` alone is **not** sufficient --
  `data_source` must be included, because this connector is explicitly
  designed to ingest more than one source over time (`models/final/
  schema.yml`'s `data_source` column doc: "Not restricted to a fixed
  set of values -- this connector may ingest additional data sources
  over time"), and two sources could plausibly reuse the same
  `claim_id` numbering.
- **Decision (this mapping's addition):** duplicate line numbers from a
  re-delivered/overlapping source file are resolved by exact-content
  deduplication before the uniqueness check runs
  (`claims_mapping.dedup_exact_duplicates`) -- a byte-identical repeat
  row collapses silently (idempotent re-delivery), but two rows sharing
  `(claim_id, claim_line_number)` with *different* content are a
  genuine conflict and fail the gate loudly
  (`claims_mapping.find_grain_conflicts`), never silently resolved by
  picking a "latest" row.
- Versioned claims/resubmissions (adjustments, voids) are **not**
  additional lines under the same `claim_id` -- see decision 2. They
  arrive as new `claim_id` values that reference the original via
  `orig_clm_id`, so the grain itself never has to represent claim
  versioning.
- **Test proving uniqueness:** `test_claims_mapping.py`'s
  `TestGrainUniqueness.test_no_grain_conflicts_in_representative_sample`
  asserts `find_grain_conflicts()` returns empty against the
  representative sample (which includes a byte-identical duplicate
  delivery of `clm-1001` line 1, proving the dedup step -- not merely
  the absence of conflicts -- is what makes the assertion pass).

## 2. Adjustments and reversals

- **Decision:** `clm_status_code` classifies every claim/line as one of
  three lifecycle states: `1` = original, `7` = adjustment/replacement,
  `8` = void. This is this mapping's own vocabulary (not confirmed
  against any specific vendor's actual code values -- **Unverified**
  for a real vendor; see Readiness), chosen because it mirrors the
  common three-way distinction (original / replacement / void) used
  across 837/835 claim-frequency conventions.
- **Linking identifier:** `orig_clm_id` -- populated only on adjustment
  and void rows, referencing the `claim_id` of the record being
  adjusted or reversed. Originals never populate it.
- **Replace vs. separate financial event, by status:**
  - `7` (adjustment/replacement): the original is **excluded** from
    net financial totals once an adjustment supersedes it -- only the
    adjustment's own amount counts. The original record itself is
    still loaded (never deleted), it is just excluded from the netting
    sum.
  - `8` (void): the original is **kept** in the net financial total,
    and the void's amount is expected to be the original's amount with
    the sign flipped (a canceling reversal), so summing both nets to
    zero. This is the opposite netting rule from adjustments,
    intentionally -- see `claims_mapping.net_paid_amount`'s docstring.
- **Sign handling:** `paid_cents`/`allowed_cents`/`charge_cents` on a
  void are negative; on an original or an adjustment they are
  non-negative. `claims_mapping.reconciliation_violations` compares by
  absolute value specifically so a void's negative triple still has to
  satisfy `|paid| <= |allowed| <= |charge|`.
- **Tests preventing double counting:**
  `TestAdjustmentsAndReversals.test_adjustment_replaces_original_in_net_total`
  (asserts the `clm-1002`/`clm-1002-adj` lineage nets to the
  adjustment's amount alone, `$95.00`, not `$85.00 + $95.00`) and
  `test_void_cancels_original_in_net_total` (asserts the
  `clm-1003`/`clm-1003-void` lineage nets to `$0.00`).

## 3. Institutional versus professional claim typing

- **Source fields used:** `claim_form_code` (primary signal),
  `bill_type_code` (institutional-only field), `place_of_service_code`
  (professional-only field).
- **Deterministic rule** (`claims_mapping.derive_claim_type`), in
  order: `claim_form_code == 'UB04'` -> `institutional`;
  `claim_form_code == 'CMS1500'` -> `professional`; else
  `bill_type_code` populated -> `institutional`; else
  `place_of_service_code` populated -> `professional`; else
  `undetermined`.
- **Ambiguous/missing coverage:** a line with both `bill_type_code` and
  `place_of_service_code` populated but no recognized
  `claim_form_code` (representative sample: `clm-1013`) resolves
  `institutional` by rule precedence (bill-type over place-of-service),
  not by chance. A line with none of the three populated
  (representative sample: `clm-1012`) resolves `undetermined` rather
  than raising or guessing.
- **Facility type / provider indicators:** the representative sample's
  vendor-shaped extract does not include a separate facility-type or
  provider-type indicator field, so this mapping does not depend on
  one. **Repository-derived assumption:** if a real vendor's extract
  does supply one, it should be added as a fourth, higher-precedence
  rule ahead of `claim_form_code`, not folded silently into the
  existing three.
- `medical_claim.claim_type`'s **accepted-values test already exists**
  in this repository (`models/final/schema.yml`:
  `accepted_values: ["institutional", "professional", "undetermined"]`)
  -- `claims_mapping.ACCEPTED_CLAIM_TYPES` matches it exactly so this
  mapping's typing rule can never produce a value the Input Layer
  contract would reject.
- **Completeness test:**
  `TestClaimTyping.test_every_accepted_line_has_a_recognized_claim_type`
  asserts every accepted representative-sample line's derived
  `claim_type` is in `ACCEPTED_CLAIM_TYPES`.

## 4. Multiple diagnosis and procedure representations

- **Verified about the existing "tuva" source:** it supplies **no**
  diagnosis or procedure codes at all -- `models/final/
  medical_claim.sql` types all 51 diagnosis and 51 procedure columns
  NULL, with an explicit comment that this is deliberate, not a gap.
  There is therefore no existing repository convention for normalizing
  repeated diagnosis/procedure columns to extend.
- **Decision:** the incoming source represents diagnoses/procedures as
  repeated columns (`diag_cd_1..diag_cd_3`, `proc_cd_1..proc_cd_2` +
  matching `proc_dt_N`), one code system per line (`diag_type`,
  `proc_type` -- a single code system applies to every code on that
  line, matching the Input Layer contract's single
  `diagnosis_code_type`/`procedure_code_type` column per claim line).
- **Normalization** (`claims_mapping.normalize_diagnoses`/
  `normalize_procedures`): source column position becomes `sequence`
  (position 1 is always primary for diagnoses); exact-duplicate codes
  within the same line are deduplicated, keeping the lowest sequence
  position; procedure dates are carried through with `safe_date`
  parsing (unparseable -> NULL, never a build failure).
- **Deduplication demonstrated:** representative-sample claim
  `clm-1008` repeats `J06.9` in both `diag_cd_1` and `diag_cd_2` --
  `normalize_diagnoses` collapses it to a single sequence-1, primary
  diagnosis.
- **Tests:** `TestDiagnosisAndProcedureNormalization.
  test_sequence_positions_are_unique_and_primary_is_position_one`,
  `test_duplicate_diagnosis_codes_are_deduplicated`, and
  `test_every_diagnosis_and_procedure_traces_to_an_accepted_claim_line`
  (parent-claim coverage: no diagnosis/procedure is ever produced for a
  line that was rejected).

## 5. Member identity across files and API versions

- **Deterministic crosswalk:** `member_crosswalk.csv` (`member_key ->
  person_id`), loaded by `claims_mapping.load_crosswalk`. **Decision:**
  this is a persisted, explicitly maintained mapping (not a hash/
  formula derived from `member_key`), because a formula cannot
  represent a merge -- two `member_key` values resolving to the same
  `person_id` is an ordinary many-to-one dictionary entry, demonstrated
  in the representative sample by `mk-002` and `mk-002b` both resolving
  to `person-002`.
- **Stability across files/time/environments/API versions:**
  **Unverified** for a real vendor -- no live vendor is connected (see
  `docs/SOURCE_CONTRACT.md`), so whether `member_key` is stable across
  API versions cannot be confirmed today. This mapping's crosswalk
  design does not depend on that stability holding: because the
  crosswalk is a persisted table rather than a formula, a vendor
  reissuing `member_key` values after an API upgrade only requires
  adding new crosswalk rows, not reprocessing history.
- **Merged/split/recycled/missing/changed identifiers:**
  - Merged: two `member_key` values map to one `person_id` (see
    above) -- supported today.
  - Split: **Unverified/not yet supported** -- this crosswalk shape
    (one `member_key` -> one `person_id`) cannot represent a single
    `member_key` later needing to resolve to two different people. If
    a real vendor's data exhibits this, the crosswalk must become
    time-versioned (`member_key`, `person_id`, effective date range)
    before historical ingestion of that vendor proceeds -- flagged as
    a blocker in Readiness.
  - Recycled: **Unverified/not yet supported**, for the same reason as
    "split" -- a static crosswalk cannot distinguish two different
    people who were issued the same `member_key` at different times.
  - Missing (a `member_key` with no crosswalk row): **Decision** --
    the claim/eligibility record is **not** dropped; it is retained
    with `person_id = NULL` and `matched_member = False`
    (`TransformedClaimLine.matched_member`), excluded from the FK
    numerator, and visible for manual investigation. Representative
    sample: `clm-1009` (`member_key = mk-999`).
- **Unmatched-member handling:** claims -- retained, flagged, excluded
  from FK numerator (never silently dropped, since a claim's financial
  totals still matter for reconciliation even if identity resolution
  failed). Eligibility -- **quarantined** (excluded from the
  consolidated span output entirely), because an eligibility span
  under an unresolved identity cannot be safely attributed to any
  `person_id` at all.
- **FK coverage expectations/threshold:** **Decision** --
  `FK_COVERAGE_THRESHOLD = 0.90` (90% of distinct `member_key` values
  across claims + eligibility must resolve). This is a threshold for
  *this representative-sample gate*, not a confirmed production SLA --
  the real threshold is **Unverified** until reconciled against a real
  vendor's actual enrollment file (see Readiness). The representative
  sample is deliberately built to land exactly at this boundary (9 of
  10 distinct `member_key` values resolve == 90.00%) so the gate's
  edge behavior itself is exercised, not just the comfortable middle.

## 6. Financial units

- **paid_cents / allowed_cents / charge_cents: Decision** -- integer
  cents, USD, unsigned on original/adjustment records and negative on
  void records (see decision 2). No other currency is assumed
  (**Unverified** for a real vendor -- see Readiness).
- **Decimal precision/rounding/scale:** converted to a 2-decimal-place
  `Decimal` via `claims_mapping.cents_to_amount`, which divides by
  `Decimal(100)` and quantizes to `0.01` -- never a `float` division
  (which can introduce binary rounding error) and never integer `//`
  division (which truncates, e.g. `8501 // 100 == 85`, silently losing
  a cent). `TestFinancialUnits.
  test_cents_to_amount_uses_decimal_division_not_integer_truncation`
  asserts `cents_to_amount("8501") == Decimal("85.01")` exactly.
- **Negative amounts:** expected and meaningful on void records (see
  decision 2); a negative amount on an *original* record would be a
  data-quality anomaly this mapping does not currently flag separately
  from the general reconciliation check.
- **Nulls:** a blank/unparseable cents value becomes `None` (pending or
  not-yet-adjudicated), never `0` -- `0` and "not yet paid" are
  different facts and must not be conflated. Representative sample:
  `clm-1005` (`paid_cents`/`allowed_cents`/`charge_cents` all blank).
- **Implausible values:** caught by the reconciliation rule below; a
  value that fails the strict digits-only pattern (not demonstrated in
  the current sample) becomes `None` via the same "malformed ->
  NULL, never a crash" policy as `macros/safe_cast.sql`.
- **Component reconciliation:** `claims_mapping.
  reconciliation_violations` requires `|paid_amount| <= |allowed_amount|
  <= |charge_amount|` on every accepted line; the representative sample
  is constructed so this holds throughout, including for the void
  line's negative triple.
- **paid_cents -> paid_amount confirmed exact:** `cents_to_amount` is
  the single conversion function used for `paid_cents`, `allowed_cents`,
  and `charge_cents` alike; `TestFinancialUnits.
  test_paid_cents_converts_to_paid_amount_without_truncation` exercises
  it directly against representative-sample values (`8500 -> 85.00`,
  `450000 -> 4500.00`) and against the boundary case above.

## 7. Eligibility period consolidation

- **Partitioning keys (Decision):** `(person_id, payer, plan,
  coverage_type)`. `coverage_type` has no destination in the Input
  Layer eligibility contract (`models/final/schema.yml`'s `eligibility`
  columns do not include it) -- it exists purely so that, if a member
  has concurrent medical and dental coverage under the same
  payer/plan, those two products are never merged into a single span.
- **Consolidation algorithm** (`claims_mapping.consolidate_eligibility`):
  within a partition, exact-duplicate spans collapse to one
  (representative sample: `mk-004`'s identical span repeated twice);
  remaining closed spans are sorted by `start_date` and merged when the
  next span's `start_date` is on or before the prior span's `end_date +
  1 day` -- i.e. **overlapping or touching (adjacent, no gap day
  between them)** counts as the same continuous coverage period and
  merges; a real gap of one or more days keeps the spans separate.
- **Adjacency demonstrated:** `mk-002`'s two spans (`2025-01-01 to
  2025-03-31` and `2025-04-01 to 2025-06-30`) touch exactly (no gap
  day) and merge into one `2025-01-01 to 2025-06-30` span.
  `mk-003`'s two spans (`...01-31` then `...04-01`) have a real gap
  (February and March) and stay as two spans -- a "meaningful coverage
  change" (a real lapse) is preserved, not silently bridged.
- **Open-ended dates:** a blank `span_end_dt` is preserved as `NULL`
  (ongoing coverage), sorts after every closed span in its partition,
  and is never merged into a closed span or treated as invalid.
  Representative sample: `mk-006`.
- **Invalid date ranges:** `span_end_dt < span_start_dt` is quarantined
  (excluded from the consolidated output, with a reason naming the
  field), never silently accepted or auto-corrected. Representative
  sample: `mk-005`.
- **Tests:** `TestEligibilityConsolidation.
  test_overlapping_spans_merge`, `test_adjacent_spans_merge`,
  `test_gapped_spans_stay_separate`, `test_duplicate_spans_collapse`,
  `test_open_ended_span_is_preserved`,
  `test_invalid_range_is_quarantined_not_loaded`, and
  `test_no_overlaps_in_consolidated_output` (the overlap test asserts
  `claims_mapping.find_span_overlaps` returns empty across the entire
  representative sample's consolidated output, not just for one
  partition).

## 8. Code systems and zero-padding

- **Code systems present in the representative sample:** diagnosis --
  `ICD-10-CM` (`diag_type`); procedure -- `ICD-10-PCS` (institutional)
  and `HCPCS` (professional) (`proc_type`); revenue center --
  `rev_code` (UB-04 numeric-looking string); bill type -- UB-04
  type-of-bill (`bill_type_code`); place of service -- CMS-1500 POS
  (`place_of_service_code`).
- **Normalization without destroying leading zeros (Decision, the
  single most load-bearing rule in this section):** every coded field
  above is `trim()`-ed and kept as `str` -- **never** cast to `int`/
  `numeric`. `claims_mapping.py` has no numeric cast anywhere in the
  code-field path. Representative sample `clm-1006`'s `rev_code =
  "0001"` and `bill_type_code = "011"` prove this: `int("0001") == 1`
  would silently destroy the value's meaning, so the mapping never
  performs that cast. `TestCodeSystemsAndZeroPadding.
  test_rev_code_and_bill_type_code_retain_leading_zeros_as_strings`
  asserts both the type (`str`) and the exact original text.
  `diagnosis_code_N`/`procedure_code_N`/`revenue_center_code`/
  `bill_type_code`/`place_of_service_code` are correspondingly declared
  `::text` in `models/final/medical_claim.sql` already -- this mapping
  is consistent with that existing contract, not introducing a new one.
- **Trimming/case/punctuation:** `trim()` (whitespace only); this
  mapping does not upper/lower-case codes or strip internal punctuation
  (e.g. an ICD-10 decimal point in `E11.9`) -- codes are expected
  pre-formatted by the source, matching how `macros/raw_field.sql`
  already treats every other text field in this repository (trim +
  empty-to-NULL, nothing more).
- **Decimal insertion/removal:** not performed. If a real vendor
  delivers diagnosis codes without the decimal (`E119` instead of
  `E11.9`), inserting it correctly requires code-length/category-aware
  logic this mapping does not implement -- **Unverified/blocker** for
  that vendor shape specifically (see Readiness); the representative
  sample only exercises already-decimalized codes.
- **Validation:** no code-format regex validation is applied beyond
  "non-empty after trim" -- a malformed code is loaded as-is (visible
  for downstream data-quality review) rather than silently dropped,
  consistent with this repository's general "typed NULL or the real
  value, never a guess" philosophy (`README.md` "Known limitations").

## Representative-sample coverage

`tests/fixtures/claims_mapping_sample/` is synthetic, contains no PHI,
and covers every applicable item from the task's representative-sample
checklist:

| # | Scenario | Where |
| - | -------- | ----- |
| 1 | Institutional and professional claims | `clm-1001`/`clm-1003`/`clm-1006`/`clm-1007`/`clm-1013` (institutional); `clm-1002`/`clm-1004`/`clm-1005`/`clm-1008`/`clm-1009`/`clm-1011` (professional) |
| 2 | Multi-line claims | `clm-1001` lines 1-2 |
| 3 | Adjustments, voids, reversals | `clm-1002`/`clm-1002-adj` (adjustment); `clm-1003`/`clm-1003-void` (void) |
| 4 | Positive, zero, negative, null financial values | `clm-1001` (positive), `clm-1004` (zero), `clm-1003-void` (negative), `clm-1005` (null) |
| 5 | Multiple diagnoses and procedures | `clm-1007` (3 diagnoses, 2 procedures); `clm-1008` (duplicate diagnosis) |
| 6 | Leading-zero codes | `clm-1006` (`rev_code="0001"`, `bill_type_code="011"`) |
| 7 | Members across multiple files | `mk-001`..`mk-006` appear in both `claims_sample.csv` and `eligibility_sample.csv` |
| 7b | Members across API versions | **Not demonstrated -- no versioned API exists for any connected vendor yet (Unverified, see `docs/SOURCE_CONTRACT.md`)** |
| 8 | Matched and unmatched member identifiers | `mk-001`..`mk-008`/`mk-002b` (matched); `mk-999` (unmatched, `clm-1009`) |
| 9 | Duplicate/overlapping source deliveries | `clm-1001` line 1 repeated verbatim |
| 10 | Eligibility periods that overlap/touch/gap | `mk-001` (overlap), `mk-002` (touch/adjacent), `mk-003` (gap) |
| 11 | Missing optional values | `clm-1005` (financials), `clm-1012` (all typing/coding fields) |
| 12 | Invalid/quarantined records | `clm-1010` (invalid `line_no`), blank `clm_id` row, `mk-005` (invalid date range) |

Item 7b is the one checklist entry this sample cannot cover, because no
real vendor or API version is connected yet -- see `docs/
SOURCE_CONTRACT.md`. It is called out explicitly rather than silently
omitted.

## Readiness

**The representative-sample mapping gate** (`claims_mapping.
historical_ingestion_ready()`) may pass: every rule documented above is
implemented and exercised against the synthetic representative sample,
and `tests/unit/test_claims_mapping.py` proves each one individually as
well as the combined gate.

**Full historical ingestion against a real vendor remains blocked**,
independent of the sample gate's result, for reasons already
established in `docs/SOURCE_CONTRACT.md` (no vendor connected, PHI
storage controls unverified, backfill volume unverified, reconciliation
tolerances unverified) plus these mapping-specific blockers:

1. `clm_status_code`'s vocabulary (`1`/`7`/`8`) and the adjustment-vs-
   void netting rules in decision 2 are this implementation's design,
   not a confirmed real-vendor convention -- **must be reconciled
   against the actual vendor's claim-status/frequency-code values
   before historical ingestion.**
2. The member crosswalk cannot yet represent identifier splits or
   reuse (decision 5) -- if the real vendor's member IDs are ever
   split or recycled, the crosswalk must become time-versioned first.
3. The 90% FK-coverage threshold (decision 5) is a sample-gate
   convenience value, not a reconciled production SLA.
4. Diagnosis/procedure codes delivered without an expected decimal
   point (decision 8) are not normalized and would load as-is --
   confirm the real vendor's code formatting before relying on
   diagnosis/procedure fields downstream.
5. No PHI-shaped value has been added anywhere in this deliverable
   (fixtures are synthetic) -- see `docs/SOURCE_CONTRACT.md` section
   13 for the still-open PHI storage/encryption/retention blockers
   that apply once a real feed is connected.

This task does not start, schedule, or enable any real extraction --
the representative-sample gate above is a readiness *signal* only.

## Last verified

2026-08-16, against this repository's `codex/add-source-to-tuva-claims-mapping`
branch (based on commit `b138f8b`). Re-verify (and update this date)
whenever `src/tuva_ingest/claims_mapping.py`, `models/final/
medical_claim.sql`/`models/final/eligibility.sql`, or the connected
upstream source changes.
