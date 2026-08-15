# API manifest contract

This repository does not integrate with a specific, named upstream vendor
API. Instead, `TUVA_API_MANIFEST_URL` points at a versioned JSON document
("the manifest") that describes one snapshot and its per-table CSV
artifacts. Any HTTP server -- a real vendor API, an internal file service,
or (in tests) an in-process mock server -- can serve this contract.

Implemented in `src/tuva_postgres/manifest.py`; validated by
`tests/unit/test_manifest.py`.

## Shape

```json
{
  "version": 1,
  "source": "tuva",
  "snapshot_id": "2026-08-14T060000Z",
  "created_at": "2026-08-14T06:00:00Z",
  "artifacts": [
    {
      "table": "patient",
      "url": "https://example.invalid/snapshots/2026-08-14T060000Z/patient.csv",
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
| `artifacts` | Exactly one entry for every table in `MANAGED_TABLES` (currently sourced from `scripts/load_to_postgres.sh`'s `MANAGED_TABLES` array -- see `manifest.py`'s `MANAGED_TABLES` and its sync test). No unknown tables, no duplicates. |
| `artifacts[].table` | Lowercase `[a-z][a-z0-9_]*`; must be one of the managed tables. |
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
   `docs/RUNBOOK.md` and `landing.py` for the full landing-layer contract).

## Local testing

`tests/unit/test_api_client.py` and `tests/integration/test_pipeline_integration.py`
run a real `http.server`-based mock server implementing this contract --
no real vendor, no network access, no credentials required.
