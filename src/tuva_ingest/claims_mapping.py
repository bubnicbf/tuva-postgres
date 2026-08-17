"""Executable implementation of the source-to-Tuva claims mapping and its
representative-sample readiness gate.

This module is deliberately independent of the dbt Input Layer pipeline
(`models/staging/`, `models/final/`) and of the `raw`/`ingest_ops`
PostgreSQL schemas. It exists to make `docs/CLAIMS_MAPPING.csv` and
`docs/CLAIMS_MAPPING_DECISIONS.md` executable and testable *before* any
real historical ingestion is attempted, per this repository's incoming-
source onboarding effort -- see `docs/SOURCE_CONTRACT.md` for why no
concrete vendor is connected yet. Nothing in this module reads from or
writes to `RAW_DATA_DIR`, the `raw`/`ingest_ops` schemas, or the
`TUVA_API_*` client; it only ever operates on the synthetic
representative sample under `tests/fixtures/claims_mapping_sample/`
(see `tests/unit/test_claims_mapping.py`).

Column and field names throughout this module match `docs/
CLAIMS_MAPPING.csv` exactly. Every transformation/DQ function below is
cited by name from that CSV's "DQ rule" column or from
`docs/CLAIMS_MAPPING_DECISIONS.md`, so the mapping sheet, this code, and
the tests that exercise it cannot silently drift apart.

`historical_ingestion_ready()` is the single readiness gate this task
requires: it returns `(False, reasons)` unless the representative
sample passes every mandatory mapping/DQ check documented below AND
`docs/CLAIMS_MAPPING.csv`/`docs/CLAIMS_MAPPING_DECISIONS.md` are free of
unresolved placeholders. A `True` result means only that this
connector's *documented mapping logic* is validated against the sample
-- it does not start, schedule, or authorize any real extraction. See
`docs/CLAIMS_MAPPING_DECISIONS.md` "Readiness" for the full statement of
what remains blocked regardless of this gate's result.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DIR = REPO_ROOT / "tests" / "fixtures" / "claims_mapping_sample"
CLAIMS_SAMPLE_PATH = SAMPLE_DIR / "claims_sample.csv"
ELIGIBILITY_SAMPLE_PATH = SAMPLE_DIR / "eligibility_sample.csv"
MEMBER_CROSSWALK_PATH = SAMPLE_DIR / "member_crosswalk.csv"

# Decision (docs/CLAIMS_MAPPING_DECISIONS.md, "Member identity"): the
# representative-sample gate requires at least this fraction of distinct
# member_key values (across claims + eligibility) to resolve through the
# crosswalk. This is a gate threshold for THIS sample, not a claim about
# any real vendor's match rate (Unverified in production -- see the
# decisions doc's Readiness section).
FK_COVERAGE_THRESHOLD = Decimal("0.90")

# Decision: accepted values for the derived claim_type field, identical
# to the existing Input Layer contract's accepted_values test
# (models/final/schema.yml's `medical_claim.claim_type`).
ACCEPTED_CLAIM_TYPES = frozenset({"institutional", "professional", "undetermined"})

# Decision: claim-status-code vocabulary this mapping recognizes (see
# docs/CLAIMS_MAPPING_DECISIONS.md, "Adjustments and reversals").
STATUS_ORIGINAL = "1"
STATUS_ADJUSTMENT = "7"
STATUS_VOID = "8"


# --------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------

def trim(value: str | None) -> str | None:
    """Trim and normalize an empty/whitespace-only string to None --
    identical policy to macros/raw_field.sql's nullif(trim(...), '')."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


_INTEGER_RE = re.compile(r"^-?[0-9]+$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def safe_int(value: str | None) -> int | None:
    """Cast to int only if the trimmed value is an unambiguous integer
    literal; otherwise None (mirrors macros/safe_cast.sql's
    safe_integer() -- never raises, never silently mangles)."""
    v = trim(value)
    if v is None or not _INTEGER_RE.match(v):
        return None
    return int(v)


def safe_date(value: str | None) -> date | None:
    v = trim(value)
    if v is None or not _DATE_RE.match(v):
        return None
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except ValueError:
        return None


def cents_to_amount(value: str | None) -> Decimal | None:
    """Convert an integer-cents string to a decimal dollar Decimal by
    dividing by 100 using Decimal arithmetic (never float, never
    integer //) so no precision is lost and odd cents (e.g. 8501 ->
    85.01) round-trip exactly. Blank/unparseable input is None (a
    pending/not-yet-adjudicated line), never coerced to 0."""
    v = trim(value)
    if v is None:
        return None
    if not re.match(r"^-?[0-9]+$", v):
        return None
    return (Decimal(v) / Decimal(100)).quantize(Decimal("0.01"))


# --------------------------------------------------------------------
# CSV loading
# --------------------------------------------------------------------

def _load_rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_crosswalk(path: Path = MEMBER_CROSSWALK_PATH) -> dict[str, str]:
    """member_key -> person_id. Multiple member_key values may map to
    the same person_id (a documented merge -- see docs/
    CLAIMS_MAPPING_DECISIONS.md, "Member identity"); this is an
    ordinary many-to-one dict and requires no special-case code."""
    rows = _load_rows(path)
    crosswalk: dict[str, str] = {}
    for row in rows:
        key = trim(row.get("member_key"))
        person_id = trim(row.get("person_id"))
        if key and person_id:
            crosswalk[key] = person_id
    return crosswalk


# --------------------------------------------------------------------
# Claim typing (docs/CLAIMS_MAPPING_DECISIONS.md, decision 3)
# --------------------------------------------------------------------

def derive_claim_type(row: dict) -> str:
    """Deterministic precedence, evaluated in this exact order:

    1. claim_form_code == 'UB04' -> institutional; == 'CMS1500' -> professional
       (an explicit form-type indicator is the most authoritative signal
       when present).
    2. Otherwise, bill_type_code populated -> institutional (a UB-04
       type-of-bill code only exists on institutional claims).
    3. Otherwise, place_of_service_code populated -> professional (a
       CMS-1500 POS code only exists on professional claims).
    4. Otherwise -> 'undetermined' (never NULL -- matches models/final/
       schema.yml's existing accepted_values test, which already
       includes 'undetermined').

    Ambiguous input (both bill_type_code and place_of_service_code
    populated, no recognized claim_form_code) resolves institutional,
    by rule 2's precedence over rule 3 -- documented, not incidental.
    """
    form = trim(row.get("claim_form_code"))
    if form == "UB04":
        return "institutional"
    if form == "CMS1500":
        return "professional"
    if trim(row.get("bill_type_code")) is not None:
        return "institutional"
    if trim(row.get("place_of_service_code")) is not None:
        return "professional"
    return "undetermined"


# --------------------------------------------------------------------
# Diagnosis / procedure normalization (decision 4)
# --------------------------------------------------------------------

@dataclass(frozen=True)
class DiagnosisCode:
    claim_id: str
    line_no: int
    sequence: int
    code_type: str
    code: str
    is_primary: bool


@dataclass(frozen=True)
class ProcedureCode:
    claim_id: str
    line_no: int
    sequence: int
    code_type: str
    code: str
    code_date: date | None


_DIAGNOSIS_COLUMNS = ("diag_cd_1", "diag_cd_2", "diag_cd_3")
_PROCEDURE_COLUMNS = (("proc_cd_1", "proc_dt_1"), ("proc_cd_2", "proc_dt_2"))


def normalize_diagnoses(row: dict, claim_id: str, line_no: int) -> list[DiagnosisCode]:
    """Normalize the source's repeated diag_cd_1..N columns into Tuva's
    expected one-row-per-code representation, preserving source column
    position as `sequence` (position 1 is always primary) and
    deduplicating exact repeated codes while keeping the first (lowest
    sequence) occurrence -- see docs/CLAIMS_MAPPING_DECISIONS.md,
    decision 4."""
    code_type = trim(row.get("diag_type"))
    out: list[DiagnosisCode] = []
    seen: set[str] = set()
    for position, column in enumerate(_DIAGNOSIS_COLUMNS, start=1):
        code = trim(row.get(column))
        if code is None or code in seen:
            continue
        seen.add(code)
        out.append(
            DiagnosisCode(
                claim_id=claim_id,
                line_no=line_no,
                sequence=len(out) + 1,
                code_type=code_type or "UNKNOWN_CODE_TYPE",
                code=code,
                is_primary=(len(out) == 0),
            )
        )
    return out


def normalize_procedures(row: dict, claim_id: str, line_no: int) -> list[ProcedureCode]:
    code_type = trim(row.get("proc_type"))
    out: list[ProcedureCode] = []
    seen: set[str] = set()
    for code_col, date_col in _PROCEDURE_COLUMNS:
        code = trim(row.get(code_col))
        if code is None or code in seen:
            continue
        seen.add(code)
        out.append(
            ProcedureCode(
                claim_id=claim_id,
                line_no=line_no,
                sequence=len(out) + 1,
                code_type=code_type or "UNKNOWN_CODE_TYPE",
                code=code,
                code_date=safe_date(row.get(date_col)),
            )
        )
    return out


# --------------------------------------------------------------------
# Claim-line transformation + rejection/quarantine (decision 1, 8)
# --------------------------------------------------------------------

@dataclass
class TransformedClaimLine:
    claim_id: str
    line_no: int
    person_id: str | None
    member_key: str
    matched_member: bool
    clm_status_code: str | None
    orig_clm_id: str | None
    claim_type: str
    bill_type_code: str | None
    place_of_service_code: str | None
    rev_code: str | None
    paid_amount: Decimal | None
    allowed_amount: Decimal | None
    charge_amount: Decimal | None
    diagnoses: list[DiagnosisCode] = field(default_factory=list)
    procedures: list[ProcedureCode] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class RejectedClaimRow:
    raw: dict
    reason: str
    field: str


@dataclass
class ClaimTransformResult:
    accepted: list[TransformedClaimLine]
    rejected: list[RejectedClaimRow]
    duplicate_rows_collapsed: int


def _rows_equal(a: dict, b: dict) -> bool:
    return a == b


def dedup_exact_duplicates(rows: list[dict]) -> tuple[list[dict], int]:
    """Collapse byte-for-byte duplicate rows (a re-delivered/overlapping
    source file), keeping the first occurrence. Rows that share a grain
    key (clm_id, line_no) but differ in content are NOT collapsed here
    -- they surface later as a uniqueness-check conflict (decision 1),
    which is a data-integrity problem this function must never mask."""
    seen: list[dict] = []
    out: list[dict] = []
    collapsed = 0
    for row in rows:
        if any(_rows_equal(row, existing) for existing in seen):
            collapsed += 1
            continue
        seen.append(row)
        out.append(row)
    return out, collapsed


def transform_claims(rows: list[dict], crosswalk: dict[str, str]) -> ClaimTransformResult:
    deduped, collapsed = dedup_exact_duplicates(rows)

    accepted: list[TransformedClaimLine] = []
    rejected: list[RejectedClaimRow] = []

    for row in deduped:
        claim_id = trim(row.get("clm_id"))
        if claim_id is None:
            rejected.append(RejectedClaimRow(raw=row, reason="clm_id is required but missing/blank", field="clm_id"))
            continue

        line_no = safe_int(row.get("line_no"))
        if line_no is None:
            reason = f"line_no {row.get('line_no')!r} did not cast safely to integer"
            rejected.append(RejectedClaimRow(raw=row, reason=reason, field="line_no"))
            continue

        member_key = trim(row.get("member_key"))
        if member_key is None:
            reason = "member_key is required but missing/blank"
            rejected.append(RejectedClaimRow(raw=row, reason=reason, field="member_key"))
            continue

        person_id = crosswalk.get(member_key)
        matched = person_id is not None

        accepted.append(
            TransformedClaimLine(
                claim_id=claim_id,
                line_no=line_no,
                person_id=person_id,
                member_key=member_key,
                matched_member=matched,
                clm_status_code=trim(row.get("clm_status_code")),
                orig_clm_id=trim(row.get("orig_clm_id")),
                claim_type=derive_claim_type(row),
                bill_type_code=trim(row.get("bill_type_code")),
                place_of_service_code=trim(row.get("place_of_service_code")),
                rev_code=trim(row.get("rev_code")),
                paid_amount=cents_to_amount(row.get("paid_cents")),
                allowed_amount=cents_to_amount(row.get("allowed_cents")),
                charge_amount=cents_to_amount(row.get("charge_cents")),
                diagnoses=normalize_diagnoses(row, claim_id, line_no),
                procedures=normalize_procedures(row, claim_id, line_no),
                raw=row,
            )
        )

    return ClaimTransformResult(accepted=accepted, rejected=rejected, duplicate_rows_collapsed=collapsed)


# --------------------------------------------------------------------
# Grain uniqueness (decision 1)
# --------------------------------------------------------------------

def grain_key(line: TransformedClaimLine, data_source: str = "incoming_source") -> tuple:
    return (line.claim_id, line.line_no, data_source)


def find_grain_conflicts(lines: list[TransformedClaimLine]) -> list[tuple]:
    """Return every grain key that appears more than once among accepted
    lines. `transform_claims` already collapses byte-identical
    duplicates, so any conflict found here is a genuine data-integrity
    problem (same claim_id/line_no with *different* content) that the
    representative-sample gate must fail on, never silently pick a
    winner for."""
    seen: dict[tuple, int] = {}
    for line in lines:
        key = grain_key(line)
        seen[key] = seen.get(key, 0) + 1
    return sorted(k for k, count in seen.items() if count > 1)


# --------------------------------------------------------------------
# Double-counting prevention (decision 2)
# --------------------------------------------------------------------

def net_paid_amount(lines: list[TransformedClaimLine]) -> Decimal:
    """Sum paid_amount across accepted lines with adjustment/void
    netting applied (decision 2):

    - A claim referenced as `orig_clm_id` by another line with
      clm_status_code == '7' (adjustment/replacement) is superseded --
      its own paid_amount is excluded from the total; only the
      adjustment's paid_amount counts.
    - A void (clm_status_code == '8') is never excluded -- its
      (expected-negative) paid_amount is summed alongside the original
      it references, so the two cancel to net zero.
    """
    superseded_claim_ids = {
        line.orig_clm_id for line in lines if line.clm_status_code == STATUS_ADJUSTMENT and line.orig_clm_id
    }
    total = Decimal("0")
    for line in lines:
        if line.claim_id in superseded_claim_ids and line.clm_status_code == STATUS_ORIGINAL:
            continue
        if line.paid_amount is not None:
            total += line.paid_amount
    return total


# --------------------------------------------------------------------
# Financial range/reconciliation (decision 6)
# --------------------------------------------------------------------

def reconciliation_violations(lines: list[TransformedClaimLine]) -> list[str]:
    """paid_amount <= allowed_amount <= charge_amount by absolute value
    (a void's amounts are all negative but must still satisfy the same
    magnitude ordering as its original). Returns one message per
    violation, naming the claim_id/line_no and the failed rule."""
    violations: list[str] = []
    for line in lines:
        paid, allowed, charge = line.paid_amount, line.allowed_amount, line.charge_amount
        if paid is None or allowed is None or charge is None:
            continue
        if not (abs(paid) <= abs(allowed) <= abs(charge)):
            violations.append(
                f"{line.claim_id}/{line.line_no}: reconciliation failed "
                f"(|paid_amount|={abs(paid)} <= |allowed_amount|={abs(allowed)} "
                f"<= |charge_amount|={abs(charge)} does not hold)"
            )
    return violations


# --------------------------------------------------------------------
# Member FK coverage (decision 5)
# --------------------------------------------------------------------

def fk_coverage(member_keys: set[str], crosswalk: dict[str, str]) -> tuple[int, int, Decimal]:
    matched = sum(1 for k in member_keys if k in crosswalk)
    total = len(member_keys)
    ratio = (Decimal(matched) / Decimal(total)) if total else Decimal("0")
    return matched, total, ratio


# --------------------------------------------------------------------
# Eligibility span consolidation (decision 7)
# --------------------------------------------------------------------

@dataclass(frozen=True)
class EligibilitySpan:
    person_id: str
    payer: str
    plan: str
    coverage_type: str
    start_date: date
    end_date: date | None  # None = open-ended


@dataclass
class EligibilityTransformResult:
    consolidated: list[EligibilitySpan]
    quarantined: list[RejectedClaimRow]


def _partition_key(row_person_id: str, payer: str, plan: str, coverage_type: str) -> tuple:
    return (row_person_id, payer, plan, coverage_type)


def consolidate_eligibility(rows: list[dict], crosswalk: dict[str, str]) -> EligibilityTransformResult:
    """Partition by (person_id, payer, plan, coverage_type) -- decision
    7's chosen partitioning keys -- then within each partition: drop
    exact-duplicate spans, reject end_date < start_date as invalid
    (quarantined, never silently dropped or silently accepted), sort by
    start_date, and merge spans that overlap OR are adjacent (the next
    span's start_date is on or before the prior span's end_date + 1
    day). An open-ended span (end_date is NULL) is preserved as-is and
    always sorts last within its partition (nothing can be adjacent to
    an open end)."""
    quarantined: list[RejectedClaimRow] = []
    by_partition: dict[tuple, list[EligibilitySpan]] = {}

    for row in rows:
        member_key = trim(row.get("member_key"))
        person_id = crosswalk.get(member_key) if member_key else None
        if person_id is None:
            reason = f"member_key {member_key!r} did not resolve via the crosswalk"
            quarantined.append(RejectedClaimRow(raw=row, reason=reason, field="member_key"))
            continue

        start = safe_date(row.get("span_start_dt"))
        if start is None:
            reason = "span_start_dt is required and must be a valid date"
            quarantined.append(RejectedClaimRow(raw=row, reason=reason, field="span_start_dt"))
            continue

        end_raw = trim(row.get("span_end_dt"))
        end = safe_date(row.get("span_end_dt")) if end_raw is not None else None
        if end_raw is not None and end is None:
            reason = "span_end_dt did not parse as a valid date"
            quarantined.append(RejectedClaimRow(raw=row, reason=reason, field="span_end_dt"))
            continue
        if end is not None and end < start:
            reason = f"span_end_dt {end} is before span_start_dt {start}"
            quarantined.append(RejectedClaimRow(raw=row, reason=reason, field="span_end_dt"))
            continue

        payer = trim(row.get("payer")) or ""
        plan = trim(row.get("plan")) or ""
        coverage_type = trim(row.get("coverage_type")) or ""
        key = _partition_key(person_id, payer, plan, coverage_type)
        span = EligibilitySpan(
            person_id=person_id, payer=payer, plan=plan, coverage_type=coverage_type, start_date=start, end_date=end
        )
        by_partition.setdefault(key, []).append(span)

    consolidated: list[EligibilitySpan] = []
    for key, spans in by_partition.items():
        # Exact-duplicate collapse first.
        deduped: list[EligibilitySpan] = []
        for span in spans:
            if span not in deduped:
                deduped.append(span)

        closed = sorted((s for s in deduped if s.end_date is not None), key=lambda s: s.start_date)
        open_ended = [s for s in deduped if s.end_date is None]

        merged: list[EligibilitySpan] = []
        for span in closed:
            if merged and span.start_date <= merged[-1].end_date + timedelta(days=1):
                prior = merged[-1]
                merged[-1] = EligibilitySpan(
                    person_id=prior.person_id,
                    payer=prior.payer,
                    plan=prior.plan,
                    coverage_type=prior.coverage_type,
                    start_date=prior.start_date,
                    end_date=max(prior.end_date, span.end_date),
                )
            else:
                merged.append(span)

        consolidated.extend(merged)
        consolidated.extend(open_ended)

    return EligibilityTransformResult(consolidated=consolidated, quarantined=quarantined)


def find_span_overlaps(spans: list[EligibilitySpan]) -> list[tuple]:
    """Return every pair of consolidated spans within the same partition
    that still overlap after consolidation -- must always be empty for
    the representative-sample gate to pass."""
    by_partition: dict[tuple, list[EligibilitySpan]] = {}
    for span in spans:
        key = (span.person_id, span.payer, span.plan, span.coverage_type)
        by_partition.setdefault(key, []).append(span)

    conflicts: list[tuple] = []
    for key, group in by_partition.items():
        closed = sorted((s for s in group if s.end_date is not None), key=lambda s: s.start_date)
        for a, b in zip(closed, closed[1:]):
            if b.start_date <= a.end_date:
                conflicts.append((a, b))
    return conflicts


# --------------------------------------------------------------------
# Placeholder scan (shared with docs/SOURCE_CONTRACT.md's convention)
# --------------------------------------------------------------------

PLACEHOLDER_MARKERS = [
    r"\bTODO\b",
    r"\bTBD\b",
    r"\bFIXME\b",
    r"fill this in",
    r"\bXXX\b",
    r"\bunknown\b",
    r"lorem ipsum",
]


def find_placeholders(text: str) -> list[str]:
    found = []
    for pattern in PLACEHOLDER_MARKERS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            found.append(match.group(0))
    return found


# --------------------------------------------------------------------
# Readiness gate
# --------------------------------------------------------------------

@dataclass
class ReadinessResult:
    ready: bool
    reasons: list[str]


def historical_ingestion_ready(
    mapping_sheet_path: Path = REPO_ROOT / "docs" / "CLAIMS_MAPPING.csv",
    decisions_doc_path: Path = REPO_ROOT / "docs" / "CLAIMS_MAPPING_DECISIONS.md",
) -> ReadinessResult:
    """The single readiness gate this task requires: True only if the
    representative sample passes every mandatory mapping/DQ rule AND the
    mapping sheet/decisions doc are free of unresolved placeholders.

    A True result reflects only that this connector's documented mapping
    logic is validated against the synthetic representative sample --
    see docs/CLAIMS_MAPPING_DECISIONS.md's "Readiness" section for what
    remains separately blocked (a real vendor connection, per
    docs/SOURCE_CONTRACT.md) regardless of this result. This function
    never starts, schedules, or authorizes extraction.
    """
    reasons: list[str] = []

    doc_paths = (
        (mapping_sheet_path, "docs/CLAIMS_MAPPING.csv"),
        (decisions_doc_path, "docs/CLAIMS_MAPPING_DECISIONS.md"),
    )
    for path, label in doc_paths:
        if not path.is_file():
            reasons.append(f"{label} does not exist")
            continue
        placeholders = find_placeholders(path.read_text(encoding="utf-8"))
        if placeholders:
            reasons.append(f"{label} contains unresolved placeholder(s): {placeholders}")

    if not CLAIMS_SAMPLE_PATH.is_file() or not ELIGIBILITY_SAMPLE_PATH.is_file() or not MEMBER_CROSSWALK_PATH.is_file():
        reasons.append("representative sample fixtures are missing under tests/fixtures/claims_mapping_sample/")
        return ReadinessResult(ready=False, reasons=reasons)

    crosswalk = load_crosswalk()
    claim_rows = _load_rows(CLAIMS_SAMPLE_PATH)
    eligibility_rows = _load_rows(ELIGIBILITY_SAMPLE_PATH)

    result = transform_claims(claim_rows, crosswalk)

    conflicts = find_grain_conflicts(result.accepted)
    if conflicts:
        reasons.append(f"claim_id/claim_line_number grain is not unique: {conflicts}")

    recon_violations = reconciliation_violations(result.accepted)
    reasons.extend(recon_violations)

    bad_types = sorted({line.claim_type for line in result.accepted} - ACCEPTED_CLAIM_TYPES)
    if bad_types:
        reasons.append(f"claim_type produced values outside the accepted set: {bad_types}")

    member_keys = {line.member_key for line in result.accepted} | {
        trim(row.get("member_key")) for row in eligibility_rows if trim(row.get("member_key"))
    }
    matched, total, ratio = fk_coverage(member_keys, crosswalk)
    if ratio < FK_COVERAGE_THRESHOLD:
        reasons.append(
            f"member_key -> person_id FK coverage {ratio:.2%} ({matched}/{total}) "
            f"is below the {FK_COVERAGE_THRESHOLD:.0%} threshold"
        )

    eligibility_result = consolidate_eligibility(eligibility_rows, crosswalk)
    span_conflicts = find_span_overlaps(eligibility_result.consolidated)
    if span_conflicts:
        reasons.append(f"consolidated eligibility spans still overlap: {span_conflicts}")

    rejection_fields = {r.field for r in result.rejected}
    if "line_no" not in rejection_fields:
        reasons.append("representative sample did not exercise an invalid line_no rejection")
    if "clm_id" not in rejection_fields:
        reasons.append("representative sample did not exercise a missing clm_id rejection")

    return ReadinessResult(ready=(len(reasons) == 0), reasons=reasons)
