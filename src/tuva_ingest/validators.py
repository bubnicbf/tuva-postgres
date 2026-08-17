"""Structural validation for source records, used to classify each record
of a published paginated run as either loadable into its endpoint's raw
table or quarantined (see `quarantine.py`, `paginated_loader.py`).

Scope, deliberately narrow: this module checks only the **structural**
ingestion contract documented in `docs/SOURCE_CONTRACT.md` SS3 ("Endpoints
and expected record grain") -- the record-grain identifier field(s) each
endpoint's documented natural key is built from (`person_id` for
`eligibility`; `claim_id` + `claim_line_number` for `medical_claim`/
`pharmacy_claim`). It never invents a clinical business rule and never
quarantines a record merely because an optional field is null -- see each
`_ENDPOINT_RULES` entry's own comment for exactly which fields are
checked and why, all traceable to that documented grain, not to a value
judgment made here.

Every record reaching this module has already passed
`pagination.validate_page_envelope`'s envelope-level check that it is a
JSON object -- `record_not_object` therefore cannot occur through the
normal `extract` -> `load` pipeline today (envelope validation fails the
whole run first, deliberately, since a non-object entry is a contract
violation the source itself made, not a single bad record). The reason
code is still defined and directly unit-tested against `validate_record`
so the allowlist stays complete and forward-compatible with a future
looser envelope contract.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# The fixed, stable allowlist of quarantine reason codes -- see
# `docs/SOURCE_CONTRACT.md`/README.md for the operator-facing description
# of each. Never put a raw field value into a reason code; use
# `QuarantineDecision.detail` (bounded, sanitized) for that instead.
REASON_CODES: frozenset[str] = frozenset(
    {
        "record_not_object",
        "missing_required_field",
        "invalid_required_type",
        "invalid_identifier",
        "invalid_date_format",
        "schema_validation_failed",
    }
)

_MAX_DETAIL_LENGTH = 200

# ISO-8601 calendar date, optionally with a time component -- deliberately
# permissive (this module does not judge calendar validity beyond shape;
# that is a dbt staging concern) since the only structural question here
# is "is this recognizably a date-shaped string at all".
_DATE_SHAPE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ].*)?$")

# Field names this connector's own documented grain (SOURCE_CONTRACT.md
# SS3) treats as date-shaped when present -- checked only if the field is
# present at all (never required -- an absent optional date is not a
# structural violation).
_KNOWN_DATE_FIELDS: dict[str, tuple[str, ...]] = {
    "eligibility": ("enrollment_start_date", "enrollment_end_date"),
    "medical_claim": ("claim_start_date", "claim_end_date"),
    "pharmacy_claim": ("claim_start_date", "claim_end_date"),
}

# Required identifier field(s) per endpoint, directly derived from
# docs/SOURCE_CONTRACT.md SS3's documented record grain:
#   - eligibility: "one row per member/enrollment record (natural-key
#     columns expected downstream: person_id, member_id, subscriber_id)"
#     -- person_id is the Input Layer's cross-domain grain key, so it is
#     the one field required here; member_id/subscriber_id remain
#     optional at the structural layer (not every source populates all
#     three, and requiring them would be inventing a stricter rule than
#     the documented contract states).
#   - medical_claim / pharmacy_claim: "one row per claim line (claim_id +
#     claim_line_number)" -- both are required, since together they *are*
#     the documented grain.
_REQUIRED_STRING_FIELDS: dict[str, tuple[str, ...]] = {
    "eligibility": ("person_id",),
    "medical_claim": ("claim_id",),
    "pharmacy_claim": ("claim_id",),
}
_REQUIRED_SCALAR_FIELDS: dict[str, tuple[str, ...]] = {
    "medical_claim": ("claim_line_number",),
    "pharmacy_claim": ("claim_line_number",),
}


@dataclass(frozen=True)
class QuarantineDecision:
    reason_code: str
    detail: str


def _truncate(detail: str) -> str:
    if len(detail) <= _MAX_DETAIL_LENGTH:
        return detail
    return detail[: _MAX_DETAIL_LENGTH - 3] + "..."


def validate_record(endpoint: str, record: object) -> QuarantineDecision | None:
    """Return a `QuarantineDecision` if `record` fails `endpoint`'s
    structural ingestion contract, or `None` if it is structurally valid
    (a `None` return says nothing about clinical/business correctness --
    only that this connector's own raw-load grain requirements are met).
    Never raises -- every rule violation is reported as a decision, not
    an exception, so a caller can classify many records in a loop without
    exception-handling overhead per record."""
    if not isinstance(record, dict):
        return QuarantineDecision("record_not_object", f"expected a JSON object, got {type(record).__name__}")

    for field in _REQUIRED_STRING_FIELDS.get(endpoint, ()):
        if field not in record or record[field] is None:
            return QuarantineDecision("missing_required_field", f"required field {field!r} is missing or null")
        value = record[field]
        if not isinstance(value, str):
            return QuarantineDecision(
                "invalid_required_type", f"field {field!r} must be a string, got {type(value).__name__}"
            )
        if not value.strip():
            return QuarantineDecision("invalid_identifier", f"field {field!r} must not be blank")

    for field in _REQUIRED_SCALAR_FIELDS.get(endpoint, ()):
        if field not in record or record[field] is None:
            return QuarantineDecision("missing_required_field", f"required field {field!r} is missing or null")
        value = record[field]
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return QuarantineDecision(
                "invalid_required_type", f"field {field!r} must be a string or number, got {type(value).__name__}"
            )
        if isinstance(value, str) and not value.strip():
            return QuarantineDecision("invalid_identifier", f"field {field!r} must not be blank")

    for field in _KNOWN_DATE_FIELDS.get(endpoint, ()):
        if field in record and record[field] is not None:
            value = record[field]
            if not isinstance(value, str) or not _DATE_SHAPE_RE.match(value):
                return QuarantineDecision(
                    "invalid_date_format", f"field {field!r} is not a recognizable ISO-8601 date/datetime string"
                )

    return None


def decision_detail(decision: QuarantineDecision) -> str:
    """Bounded, sanitized detail text safe to store in
    `quarantined_records.reason_detail` -- field *names* and rule
    descriptions only, never a raw field value or the complete record."""
    return _truncate(decision.detail)
