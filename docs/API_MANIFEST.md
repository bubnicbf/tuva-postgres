# API manifest contract (legacy full-manifest CSV path)

**This document describes the legacy, full-manifest CSV contract used
only by `tuva-ingest run`/`load-raw` today.** The current, primary
`extract`/`load`/`sync` commands use a different, paginated JSON
contract instead -- see `docs/SOURCE_CONTRACT.md` Section 4
("Pagination") and `src/tuva_ingest/pagination.py` for that one. Both
contracts are documented, tested, and supported; see README.md
"Backward compatibility" for which commands use which.

This repository does not integrate with a specific, named upstream vendor
API. Instead, `TUVA_API_MANIFEST_URL` points at a versioned JSON document
("the manifest") that describes one snapshot and its per-table CSV
artifacts. Any HTTP server -- a real vendor API, an internal file service,
or (in tests) `httpx.MockTransport` -- can serve this contract. The same
`TUVA_API_MANIFEST_URL` value is reused as the paginated contract's
page-request URL (see `docs/SOURCE_CONTRACT.md`) -- both contracts
request the one configured URL, just with different query parameters and
different response shapes.

Implemented in `src/tuva_ingest/manifest.py`; validated by
`tests/unit/test_manifest.py`. Fetched (with bounded, tenacity-driven
retries) by `src/tuva_ingest/api_client.py`'s `ApiClient`, built on
`httpx`.

## The legacy, un-scoped `tuva-ingest run`/`load-raw` pipeline

`tuva-ingest run` fetches one manifest containing all three raw tables'
artifacts in a single request, downloads and checksums each CSV
artifact, and loads all three tables together (`extract.extract_snapshot`,
`raw_loader.load_snapshot`). `tuva-ingest load-raw [--snapshot-id ...]`
loads an already-published snapshot the same way. Neither command
accepts `--endpoint`/`--since` -- see the paginated contract
(`docs/SOURCE_CONTRACT.md`) for endpoint-scoped, incremental extraction.

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
| `artifacts` | Exactly one entry for every table in `manifest.RAW_TABLES` (`eligibility`, `medical_claim`, `pharmacy_claim`), no more, no fewer, no duplicates, no unknown tables (`manifest.parse_and_validate`'s `expected_tables` parameter, defaulted to all three for this legacy, un-scoped flow). |
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

## Run identifiers in this legacy path

`tuva-ingest run` mints its own `run_id` (`run-<uuid4 prefix>`) for its
`ingest_ops.ingestion_runs` bookkeeping row, independent of the
manifest's own `snapshot_id`. `tuva-ingest load-raw [--snapshot-id ...]`
does the same (`load-<snapshot_id>-<random suffix>`). Neither is the
same `run_id` concept the paginated `extract`/`load`/`sync` commands use
-- see `docs/SOURCE_CONTRACT.md`/`pagination.py` for that one, where
`run_id` is what `load --run-id <value>` resolves directly.

## Local testing

`tests/unit/test_api_client.py` uses `httpx.MockTransport` (no real
socket, no live server, no external API, no credentials) to exercise
every retry/auth/checksum/query-parameter-encoding path.
`tests/integration/test_pipeline_integration.py` (a real, disposable
PostgreSQL database, never a real vendor API) proves the load/state side
of the same contract end to end.
