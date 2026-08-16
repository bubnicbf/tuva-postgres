# API manifest contract

This repository does not integrate with a specific, named upstream vendor
API. Instead, `TUVA_API_MANIFEST_URL` points at a versioned JSON document
("the manifest") that describes one snapshot and its per-table CSV
artifacts. Any HTTP server -- a real vendor API, an internal file service,
or (in tests) `httpx.MockTransport` -- can serve this contract.

Implemented in `src/tuva_ingest/manifest.py`; validated by
`tests/unit/test_manifest.py`. Fetched (with bounded, tenacity-driven
retries) by `src/tuva_ingest/api_client.py`'s `ApiClient`, built on
`httpx`.

## Endpoint-scoped requests (`extract`/`sync`)

`tuva-ingest extract --endpoint <name> [--since <date>]` and
`tuva-ingest sync --endpoint <name> [--since <date>]` request a manifest
scoped to exactly **one** endpoint at a time -- never all three raw
tables in one call. `--endpoint` and `--since` are sent as httpx query
parameters on `TUVA_API_MANIFEST_URL` (encoded by httpx itself, never
string-concatenated):

```
GET {TUVA_API_MANIFEST_URL}?endpoint=medical-claims&since=2025-01-01
Authorization: Bearer {TUVA_API_TOKEN}
```

| `--endpoint` value | Raw table (see `src/tuva_ingest/endpoints.py`) |
| --- | --- |
| `medical-claims` | `medical_claim` |
| `pharmacy-claims` | `pharmacy_claim` |
| `eligibility` | `eligibility` |

`--since` is an optional `YYYY-MM-DD` date, validated locally (rejected
before any HTTP request is made if malformed) and passed straight
through as the `since` query parameter -- this connector does not
interpret it further; the upstream source is expected to filter/paginate
by it.

For a scoped request, the manifest response's `artifacts` list must
contain **exactly one** entry -- for the one table `--endpoint` maps to,
no more, no fewer (`manifest.parse_and_validate`'s `expected_tables`
parameter enforces this; see `extract.extract_endpoint_snapshot`). The
legacy, un-scoped `tuva-ingest run` pipeline (see README.md) still
fetches one manifest containing all three tables' artifacts in a single
request -- the shape below is the same either way, only the required
`artifacts` membership differs.

The published `manifest.json` for a scoped extraction additionally
records the exact request that produced it -- `_requested_endpoint` and
`_requested_since` -- alongside the source's own fields, so a run can be
audited and reloaded deterministically from disk alone. These two keys
are added by this connector after validating the source's response; they
are never required or interpreted as part of the source's own manifest
contract.

For the operational contract of whatever source is actually configured
behind this wire format -- authentication, pagination, rate limits,
incremental/historical-mutability semantics, corrections/deletions,
backfill volume, PHI classification, and reconciliation -- see
`docs/SOURCE_CONTRACT.md`. That document must stay current before any
extraction change is considered complete (validated by
`tests/unit/test_source_contract.py`).

## Shape

```json
{
  "version": 1,
  "source": "tuva",
  "snapshot_id": "2026-08-14T060000Z",
  "created_at": "2026-08-14T06:00:00Z",
  "artifacts": [
    {
      "table": "eligibility",
      "url": "https://example.invalid/snapshots/2026-08-14T060000Z/eligibility.csv",
      "sha256": "3f786850e387550fdab836ed7e6dc881de23001b52c250d8b7fd54f8e10f0a2",
      "size_bytes": 12345
    }
  ]
}
```

## Field rules

| Field | Rule |
| --- | --- |
| `version` | Must be a currently supported integer (only `1` today). |
| `source` | Nonempty string identifying the upstream source (used as the top-level directory name under `RAW_DATA_DIR`). |
| `snapshot_id` | 1-128 chars, `[A-Za-z0-9][A-Za-z0-9_.-]*` -- must be safe to use as a filesystem directory name; no `/`, `..`, or path separators. |
| `created_at` | ISO-8601 timestamp (`Z` or explicit offset). |
| `artifacts` | Exactly one entry per *requested* table, no more, no fewer, no duplicates, no unknown tables. For a `--endpoint`-scoped request (see "Endpoint-scoped requests" above) that means exactly one artifact, for the one table requested; for the legacy un-scoped flow (`tuva-ingest run`), exactly one entry for every table in `manifest.RAW_TABLES` (`eligibility`, `medical_claim`, `pharmacy_claim`). |
| `artifacts[].table` | Lowercase `[a-z][a-z0-9_]*`; must be one of the three raw tables above. |
| `artifacts[].url` | `https://` by default; `http://` is only accepted when `TUVA_API_ALLOW_INSECURE_HTTP=1` (local tests). No `..` path-traversal segments. Must have a host. |
| `artifacts[].sha256` | Lowercase 64-character hex SHA-256 of the exact bytes the client will download. |
| `artifacts[].size_bytes` | Nonnegative integer; the exact byte count of the artifact. Verified against the actual number of bytes streamed. |

A manifest failing any rule is rejected in full (`ManifestError`, listing
every problem found) before any artifact is downloaded.

## What the client does with it

1. `GET TUVA_API_MANIFEST_URL` with `Authorization: Bearer $TUVA_API_TOKEN`.
2. Parse and validate the JSON body against the rules above.
3. If `RAW_DATA_DIR/<source>/<snapshot_id>/_SUCCESS` already exists: compare
   the stored `manifest.json` to the freshly fetched one. Identical content
   -> the fetch is a no-op (idempotent retry). Different content for the
   same `snapshot_id` -> fail loudly; a snapshot_id must be immutable once
   published.
4. Otherwise, stream each artifact (bearer-authenticated, retried,
   checksummed) into a temporary staging directory, then atomically publish
   it as `RAW_DATA_DIR/<source>/<snapshot_id>/` (see
   `docs/RUNBOOK.md` and `extract.py`'s `RawSnapshotStore` for the full
   snapshot-store contract). A published snapshot is only ever loaded into
   the configured raw warehouse schema (`raw_loader.py`) -- never directly
   into any Tuva-managed core, terminology, or output schema; dbt (see
   `models/`) is what maps it into the Tuva Input Layer.

## Run IDs and `load`/`sync`

A successful `extract` prints a JSON result to stdout including `run_id`
-- this connector reuses the manifest's own immutable `snapshot_id` as
the run id (rather than minting a second identifier to keep in sync),
since the on-disk snapshot layout is already keyed by `snapshot_id` (see
`extract.py`'s `RawSnapshotStore`). `tuva-ingest load --run-id <value>`
resolves that exact run directly from `RAW_DATA_DIR` -- no database
lookup required to find it -- verifies its `_SUCCESS` marker and
checksums, and loads only that one endpoint's raw table. `tuva-ingest
sync --endpoint ... [--since ...]` runs `extract` then `load` for the
same run in one command, and stops immediately (nonzero exit, `load`
never attempted) if `extract` fails.

## Local testing

`tests/unit/test_api_client.py` uses `httpx.MockTransport` (no real
socket, no live server, no external API, no credentials) to exercise
every retry/auth/checksum/query-parameter-encoding path.
`tests/integration/test_pipeline_integration.py` (a real, disposable
PostgreSQL database, never a real vendor API) proves the load/state side
of the same contract end to end.
