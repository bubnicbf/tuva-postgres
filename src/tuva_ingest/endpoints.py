"""The public `--endpoint` name contract for `tuva-ingest extract/load/sync`.

`tuva-ingest`'s CLI surface talks about "endpoints" (`medical-claims`,
`pharmacy-claims`, `eligibility`) -- the vocabulary an operator or a
scheduler uses. Everywhere else in this connector (the raw warehouse
schema, `manifest.RAW_TABLES`, dbt's `models/sources.yml`) uses the
underlying raw *table* name (`medical_claim`, `pharmacy_claim`,
`eligibility`). This module is the single, authoritative mapping between
the two, so no other module re-derives or hardcodes its own copy.

Endpoint names are the hyphenated, human-facing form deliberately
distinct from table names (`medical-claims` vs. `medical_claim`) so a
CLI typo against the wrong vocabulary (e.g. `--endpoint medical_claim`)
is rejected immediately with a clear, actionable error rather than
silently doing the wrong thing.
"""
from __future__ import annotations

from .errors import CliUsageError

# Keep in exact sync with `manifest.RAW_TABLES` -- every endpoint this
# connector supports maps to exactly one managed raw table, and every
# managed raw table is reachable through exactly one endpoint name.
ENDPOINT_TABLE_MAP: dict[str, str] = {
    "medical-claims": "medical_claim",
    "pharmacy-claims": "pharmacy_claim",
    "eligibility": "eligibility",
}

TABLE_ENDPOINT_MAP: dict[str, str] = {table: endpoint for endpoint, table in ENDPOINT_TABLE_MAP.items()}

SUPPORTED_ENDPOINTS: tuple[str, ...] = tuple(sorted(ENDPOINT_TABLE_MAP))


def table_for_endpoint(endpoint: str) -> str:
    """Return the raw table name for `endpoint`. Raises `CliUsageError`
    (never a bare `KeyError`) for any unsupported endpoint name, listing
    every supported endpoint so the operator can immediately self-correct."""
    try:
        return ENDPOINT_TABLE_MAP[endpoint]
    except KeyError:
        raise CliUsageError(
            f"--endpoint {endpoint!r} is not supported (supported endpoints: "
            f"{', '.join(SUPPORTED_ENDPOINTS)})"
        ) from None


def endpoint_for_table(table: str) -> str:
    """The inverse of `table_for_endpoint` -- used when resolving a
    previously published extraction (which records its raw table name)
    back to the endpoint name an operator would recognize."""
    try:
        return TABLE_ENDPOINT_MAP[table]
    except KeyError:
        raise CliUsageError(f"table {table!r} is not a managed endpoint table") from None
