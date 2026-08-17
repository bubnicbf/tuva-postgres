"""Object-key construction for the durable raw-page landing zone.

Fixed layout (see docs/SOURCE_CONTRACT.md "Object storage layout" and
README.md "Architecture"):

    <prefix>/vendor=<vendor>/endpoint=<endpoint>/load_date=<YYYY-MM-DD>/
      run_id=<uuid>/page=<six-digit-page-number>.jsonl.gz

Example::

    raw/vendor=acme/endpoint=medical_claim/load_date=2026-08-14/
      run_id=550e8400-e29b-41d4-a716-446655440000/page=000001.jsonl.gz

Every dynamic path component is validated *before* it is ever composed
into a key -- an unsafe vendor/endpoint/prefix value (path traversal,
uppercase, whitespace, a slash, ...) is rejected immediately (see
`_validate_component`), never silently sanitized or truncated. `endpoint`
is always the stable, normalized snake_case partition name
(`medical_claim`, `pharmacy_claim`, `eligibility`) -- reusing
`endpoints.table_for_endpoint`, the single authoritative
hyphenated-CLI-name -> snake_case mapping already used everywhere else in
this connector, never re-derived here.

`run_id` is always a true, randomly generated UUID4 (`new_run_id`) -- it
never embeds a timestamp or endpoint name (`load_date` and `endpoint` are
already separate, explicit key components; embedding them again inside
`run_id` would be redundant and would make `run_id` a leaky, parseable
composite key instead of an opaque identifier).
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

from .. import endpoints
from ..errors import ObjectKeyError

DEFAULT_PREFIX = "raw"
MANIFEST_FILENAME = "manifest.json"
SUCCESS_MARKER = "_SUCCESS"

# Lowercase letters/digits/underscore/hyphen only, 1-64 chars, starting
# with a letter or digit -- deliberately stricter than
# identifiers.IDENTIFIER_PATTERN (this validates an object-key *path
# segment*, not a SQL identifier: hyphens are allowed since endpoint/
# vendor names commonly use them, but '/', '.', whitespace, and any
# path-traversal-shaped substring are always rejected).
_SAFE_COMPONENT_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _validate_component(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_COMPONENT_RE.match(value) or ".." in value:
        raise ObjectKeyError(
            f"{label}={value!r} is not a safe object-key path component (expected 1-64 lowercase "
            "letters/digits/underscore/hyphen, starting with a letter or digit; no '/', '.', or "
            "whitespace)"
        )
    return value


def validate_prefix(prefix: str) -> str:
    """Validate a (possibly multi-segment) key prefix, e.g. `"raw"` or
    `"raw/landing"`. Strips leading/trailing slashes; every '/'-separated
    segment is validated independently. Never empty."""
    if not isinstance(prefix, str):
        raise ObjectKeyError(f"object storage prefix must be a string, got {type(prefix).__name__}")
    stripped = prefix.strip("/")
    if not stripped:
        raise ObjectKeyError("object storage prefix must not be empty")
    for segment in stripped.split("/"):
        _validate_component(segment, "prefix segment")
    return stripped


def normalize_endpoint(endpoint: str) -> str:
    """The stable snake_case object-key partition name for `endpoint`
    (e.g. `"medical-claims"` -> `"medical_claim"`), via
    `endpoints.table_for_endpoint` -- never re-derived ad hoc. Raises
    `errors.CliUsageError` (via `table_for_endpoint`) for an unsupported
    endpoint name."""
    return endpoints.table_for_endpoint(endpoint)


def new_run_id() -> str:
    """A fresh, randomly generated UUID4 run identifier -- see module
    docstring for why this must never embed a timestamp or endpoint
    name."""
    return str(uuid.uuid4())


def validate_run_id(run_id: object) -> str:
    if not isinstance(run_id, str) or not _UUID_RE.match(run_id.lower()):
        raise ObjectKeyError(f"run_id={run_id!r} is not a valid UUID")
    return run_id.lower()


def utc_load_date(moment: datetime | None = None) -> date:
    """The UTC calendar date `moment` (default: now) falls on -- always
    computed from a timezone-aware UTC instant, never a naive local
    clock read, so `load_date` is deterministic regardless of the host's
    local timezone."""
    if moment is None:
        moment = datetime.now(timezone.utc)
    elif moment.tzinfo is None:
        raise ObjectKeyError("load_date must be derived from a timezone-aware datetime, got a naive one")
    return moment.astimezone(timezone.utc).date()


@dataclass(frozen=True)
class RunKey:
    """Every object key for one extraction run, all sharing one
    `run_prefix`. Immutable and cheap to pass around -- `object_extract.py`
    builds exactly one of these per run and threads it through
    publication."""

    prefix: str
    vendor: str
    endpoint: str
    load_date: date
    run_id: str

    @property
    def run_prefix(self) -> str:
        return (
            f"{self.prefix}/vendor={self.vendor}/endpoint={self.endpoint}/"
            f"load_date={self.load_date.isoformat()}/run_id={self.run_id}"
        )

    def page_key(self, page_number: int) -> str:
        if isinstance(page_number, bool) or not isinstance(page_number, int) or not (1 <= page_number <= 999_999):
            raise ObjectKeyError(f"page_number={page_number!r} must be an integer between 1 and 999999 inclusive")
        return f"{self.run_prefix}/page={page_number:06d}.jsonl.gz"

    @property
    def manifest_key(self) -> str:
        return f"{self.run_prefix}/{MANIFEST_FILENAME}"

    @property
    def success_key(self) -> str:
        return f"{self.run_prefix}/{SUCCESS_MARKER}"


def build_run_key(
    *, prefix: str = DEFAULT_PREFIX, vendor: str, endpoint: str, load_date: date, run_id: str
) -> RunKey:
    """Validate every component and return the `RunKey` for one
    extraction run. `endpoint` must already be the normalized snake_case
    form (see `normalize_endpoint`) -- callers normalize explicitly at
    the CLI boundary rather than having this function silently normalize
    an already-hyphenated value, so a caller that forgets to normalize
    fails loudly here instead of writing a key that mixes conventions."""
    return RunKey(
        prefix=validate_prefix(prefix),
        vendor=_validate_component(vendor, "vendor"),
        endpoint=_validate_component(endpoint, "endpoint"),
        load_date=load_date,
        run_id=validate_run_id(run_id),
    )
