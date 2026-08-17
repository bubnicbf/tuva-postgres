# Source contract: the `tuva-ingest` extraction source

This document is the operational source contract for whatever upstream
HTTP source is configured as this connector's extraction target
(`TUVA_API_MANIFEST_URL` / `SOURCE_NAME`, see `src/tuva_ingest/config.py`
and `scripts/setup_env.example`). It exists so that no extraction code
change ships without the vendor-facing facts below being written down,
checked, and kept current -- see "Readiness" and "Automated validation".

It complements, and does not replace, `docs/API_MANIFEST.md` (the wire
format only) and `docs/RUNBOOK.md` (day-to-day operation). Implementation:
`src/tuva_ingest/{config,api_client,manifest,extract,raw_loader,state}.py`
for the wire/legacy contract; `src/tuva_ingest/{object_storage/,
object_extract,object_raw_loader,endpoint_contract,schema_observation}.py`
and `migrations/006_object_storage_raw_contract.sql` for the object-storage-
backed contract (Section 15). Validated by: `tests/unit/test_source_contract.py`,
`tests/unit/test_object_storage_*.py`, `tests/unit/test_endpoint_contract.py`,
`tests/unit/test_schema_observation.py`, `tests/integration/test_object_storage_*.py`.

## A repository-derived fact this document leads with

`docs/API_MANIFEST.md` states plainly: "This repository does not
integrate with a specific, named upstream vendor API." The connector is
deliberately vendor-agnostic -- `SOURCE_NAME` (default `"tuva"`) and
`TUVA_API_MANIFEST_URL` are supplied per deployment, and any HTTP server
implementing the manifest contract (a real vendor, an internal file
service, or a test mock) can be configured as the source. No concrete
external vendor is connected in this repository today.

Per this task's instructions, vendor-specific facts that cannot be
verified from repository code or from an authoritative, currently
connected vendor's documentation are marked **Unverified** below rather
than guessed. Facts about the connector's own, already-implemented
behavior are marked **Verified** with a code citation. Facts this
repository has deliberately chosen (independent of any vendor) are
marked **Decision**. Reasonable inferences that still need vendor
confirmation are marked **Repository-derived assumption**.

## Status key

| Tag | Meaning |
| --- | --- |
| **Verified** | Confirmed directly from this repository's code, tests, or migrations (citation given). |
| **Repository-derived assumption** | A reasonable inference from repository structure that still needs vendor confirmation. |
| **Unverified** | Cannot be confirmed from this repository or an authoritative, currently connected vendor source. Explained, not guessed. |
| **Decision** | A choice this implementation makes that does not depend on vendor behavior. |

## 1. Base URL and API version

- Production base URL: **Unverified / not a fixed constant.** `TUVA_API_MANIFEST_URL` is a full URL supplied per deployment via environment variable (`config.py`); no hostname or path prefix is hardcoded anywhere in `src/tuva_ingest/`. **Decision:** this is deliberate -- see "A repository-derived fact" above.
- Environment-specific URLs: **Unverified.** No sandbox/production URL convention exists in the repository. `PIPELINE_ENVIRONMENT` (`config.py`) only labels operational metadata (`ingest_ops.ingestion_runs.environment`); it never selects a URL.
- API version and how it is selected: **Verified.** The manifest's wire-format version is the `version` field inside the fetched JSON body itself, checked against `SUPPORTED_MANIFEST_VERSIONS = (1,)` (`manifest.py`). There is no URL path segment (e.g. `/v1/`) or version header.
- Version deprecation: **Unverified.** No deprecation policy exists for manifest version 1. Introducing version 2 requires a deliberate, reviewed update to `SUPPORTED_MANIFEST_VERSIONS` (see `docs/RUNBOOK.md` "Upgrading" for the analogous single-commit convention used for the pinned Tuva package).

## 2. Authentication

- Mechanism: **Verified.** Static bearer token, `Authorization: Bearer {TUVA_API_TOKEN}`, sent on every manifest fetch and every artifact download (`ApiClient._headers()`, `api_client.py`).
- Required headers: **Verified.** `Authorization`, `User-Agent: tuva-ingest/{__version__}`, `Accept: application/json, text/csv;q=0.9, */*;q=0.1` (`api_client.py`).
- Scopes, token endpoint, signing rules: **Unverified.** No OAuth flow, token endpoint, or request signing exists in the code -- the token is a pre-issued opaque bearer credential supplied via environment variable. **Decision/gap:** a vendor that requires OAuth client-credentials with expiring tokens is not yet supported; `ApiClient` has no refresh capability today.
- Token lifetime: **Unverified.**
- Secret management: **Verified.** For the paginated `extract`/`load`/`sync` commands, the credential is retrieved at runtime from a configured secret provider (`src/tuva_ingest/secrets.py`), never read from a plaintext `.env` value directly except through the `"env"` provider (the default, kept for full backward compatibility -- it reads `TUVA_API_TOKEN`, same as before). The `"aws"` provider retrieves the credential from AWS Secrets Manager via `boto3`, authenticating with ambient identity only (an IAM role, an assumed role, `AWS_PROFILE`, or a local developer profile) -- this connector never accepts or configures a static AWS access key. The secret is retrieved exactly once per process/run, never once per page, and never written to disk. `TUVA_API_SECRET_PROVIDER`/`TUVA_API_SECRET_ID`/`AWS_REGION` are non-secret lookup information only (`scripts/setup_env.example`). The legacy `run`/`load-raw` commands still read `TUVA_API_TOKEN` directly via `config.api_token_value` (unchanged). `.env` is git-ignored (`.gitignore`); `IngestConfig.safe_dict()`/`__repr__` redact `api_token`/`pg_dsn` as `"***REDACTED***"`; `secrets.ApiCredential.api_token` is a `pydantic.SecretStr`, never a bare `str`, so it can never leak through an accidental `print()`/log call either. `docs/RUNBOOK.md` "Security notes" documents rotation via `.env`/secret store with no code change required.
- Secrets and PHI must never be committed to this repository. Examples below are redacted placeholders only, consistent with `scripts/setup_env.example`'s empty-string convention -- no real token, DSN, or patient data appears in this document.

Redacted example (never a real value):

```
export TUVA_API_MANIFEST_URL="https://example.invalid/snapshots/latest/manifest.json"
export TUVA_API_TOKEN="<redacted>"
```

## 3. Endpoints and expected record grain

Three endpoint *shapes*, not fixed paths (**Verified**, `api_client.py` / `manifest.py` / `pagination.py`):

1. `GET {TUVA_API_MANIFEST_URL}?endpoint=<name>&since=<watermark>&page_token=<token>&page_size=<n>` -- the paginated JSON page request (see Section 4 below). Purpose: incrementally extract one endpoint's records, one page at a time. Grain: one JSON object (a page of records plus metadata) per request; this is the mechanism `extract`/`load`/`sync` use today.
2. `GET {TUVA_API_MANIFEST_URL}` (legacy, no query parameters) -- returns the full manifest JSON document (see `docs/API_MANIFEST.md` "Shape", reproduced below). Purpose: enumerate one snapshot's per-table CSV artifacts and their checksums. Grain: one document per snapshot, not itself record-grained. Still used by `run`/`load-raw`.
3. `GET {artifact.url}` (one per table, URL supplied inside the legacy manifest, never a fixed path) -- returns one complete CSV file for that table. Purpose: full extract of one raw table's current complete contents (legacy contract only).

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

Expected record grain per table (**Verified**, `models/sources.yml` + `tests/unit/test_input_layer_contract.py`):

- `eligibility`: one row per member/enrollment record (natural-key columns expected downstream: `person_id`, `member_id`, `subscriber_id`).
- `medical_claim`: one row per claim **line** (`claim_id` + `claim_line_number`).
- `pharmacy_claim`: one row per claim **line** (`claim_id` + `claim_line_number`).

Primary/natural identifiers: **Verified** that every `models/final/*.sql` model declares exactly one `dbt_utils.unique_combination_of_columns` test that includes `data_source` (`models/final/schema.yml`, enforced by `tests/unit/test_input_layer_contract.py`'s `TestSchemaYamlCoversFinalModels`) -- the connector's designed dedup key is a composite of business-key columns plus `data_source`, not a bare vendor ID. Exact per-table column lists are intentionally not duplicated here; read `models/final/schema.yml` directly to avoid drift.

Parent-child relationships: **Verified.** claim (`claim_id`) -> claim line (`claim_id` + `claim_line_number`) for `medical_claim`/`pharmacy_claim`. `eligibility` has no child table in this connector.

Detail/follow-up endpoints: **Verified absent.** The manifest must list exactly the three `manifest.RAW_TABLES` artifacts (`manifest.py` rejects unknown or missing tables); there is no separate "claim detail" or "member detail" follow-up call anywhere in this codebase.

## 4. Pagination

- Mechanism: **Verified.** `extract`/`load`/`sync` (the current, primary extraction path) request one JSON page at a time from `TUVA_API_MANIFEST_URL` (reused as the page-request URL) and continue via `next_page_token` until the source explicitly signals completion (`src/tuva_ingest/pagination.py`). The legacy `run`/`load-raw` full-manifest CSV path (Sections above/`docs/API_MANIFEST.md`) genuinely does not paginate -- one complete CSV file per table per snapshot -- and both mechanisms coexist unchanged in this codebase (see README.md "Backward compatibility").
- Response envelope (**Decision** -- this repository does not integrate a specific named vendor, so these are this connector's own fixed, documented field names, chosen here since none were established elsewhere):

  ```json
  {
    "records": [{"...": "..."}],
    "metadata": {
      "record_count": 1,
      "page_token": "eyJvZmZzZXQiOjB9",
      "next_page_token": "eyJvZmZzZXQiOjEwfQ==",
      "high_water_mark": "2025-06-01T00:00:00Z"
    }
  }
  ```

- Request parameters: **Verified.** `endpoint`, `since` (the prior committed watermark, or an explicit `--since` override), `page_token`, and `page_size` (`TUVA_API_PAGE_SIZE`, optional) are always sent as real httpx query parameters (`ApiClient.get_json_page`), never concatenated into the URL (`pagination.extract_paginated_run`).
- Response fields: **Verified**, validated before anything is written to disk (`pagination.validate_page_envelope`): `records` must be a JSON array of JSON objects; `metadata.record_count` must be a non-negative integer equal to the actual number of records; a returned `metadata.page_token` (when present) must match the requested token; `metadata.next_page_token` is null/absent only on the final page and, when present, must be a non-empty string; `metadata.high_water_mark` is required and must be a non-empty string.
- Termination condition: **Verified.** `metadata.next_page_token` being null or absent. A hard safety ceiling (`TUVA_API_MAX_PAGES`, default 10,000) additionally aborts pagination that never terminates, and every requested/returned token is tracked for the run's lifetime so a repeated token (a pagination cycle) fails loudly (`PaginationError`) rather than looping forever (`pagination.py`).
- Ordering guarantees: **Unverified.** The paginated contract's within-page record order, and whether pages themselves are guaranteed non-overlapping/gap-free by any real, eventually-connected vendor, are not established anywhere in this repository -- this connector requests pages strictly in the order the source's own `next_page_token` chain dictates and never reorders or re-sorts records itself.
- Token expiry / safe restart: **Unverified** for a real vendor (whether a `page_token`/`next_page_token` value expires, and after how long). This connector holds no long-lived pagination state across process restarts -- a killed/restarted `extract` simply starts a fresh run from page 1 using the current watermark; a `load`/`sync` failure never leaves an inconsistent watermark (Section 14 below; `state.get_watermark`/`commit_watermark`).
- Duplicate/missing records across boundaries: **Decision.** Loading is idempotent per run (`_snapshot_id`, `_source_row_number` unique index -- `migrations/005_paginated_extraction_state.sql`), so repeating a load never duplicates rows within one run; whether a real vendor's own pagination can itself produce duplicate or missing records across two different runs' page boundaries (e.g. under concurrent writes on the source side) is **Unverified**, restated in Readiness.
- **Decision, restated in Readiness:** the specific real vendor eventually connected must be confirmed to implement this exact minimal envelope (or this section, `pagination.py`, and `docs/API_MANIFEST.md` must be revised together) before extraction against that vendor is considered ready.

## 5. Rate limits and retry behavior

- Published or observed vendor limits: **Unverified.** No specific vendor is connected; nothing in this repository documents an actual published rate limit.
- Client-side retry policy (**Verified**, `api_client.py`):
  - Retryable statuses: exactly `{429, 500, 502, 503, 504}` (`RETRYABLE_STATUS`). Every other 4xx (401, 403, 404, etc.) fails immediately and is never retried.
  - `Retry-After`: parsed as a plain float number of seconds only (`_parse_retry_after`); the HTTP-date form is explicitly not implemented and falls back to exponential backoff (a known, documented gap, not an oversight).
  - Backoff: exponential, `min(2**attempt, 30)` seconds base, plus uniform jitter up to 25% of the base (`_backoff_seconds`).
  - Maximum retries: `TUVA_API_MAX_RETRIES`, default `5` (`config.py`, `scripts/setup_env.example`), applied per HTTP request (one manifest fetch or one artifact download), not per whole extraction run.
  - Non-retryable errors: 401/403 raise `DownloadError` immediately with a "not authorized" message that never includes the token; 404 raises immediately as "not found"; any other non-2xx, non-retryable status raises immediately.
- Response headers consulted: **Verified** -- only `Retry-After`. No `X-RateLimit-*`/`RateLimit-*` headers are read anywhere in the client.
- **Decision:** this policy is generic and defensive by design, not tuned to a specific vendor's published limits. Whether it provides sufficient headroom for a real vendor is **Unverified** until one is connected and load-tested.

## 6. Incremental extraction field

**Verified:** the paginated `extract`/`load`/`sync` path performs true incremental extraction, keyed by an opaque, source-supplied **high-water mark** -- not a connector-chosen field name/column. Every page response's `metadata.high_water_mark` is a candidate value for the *next* incremental run; this connector never inspects, types, or filters by any specific row-level field itself (e.g. no hardcoded `updated_at` column) -- the source alone decides what "since `X`" means and returns accordingly (`pagination.py`, `docs/SOURCE_CONTRACT.md` Section 4). The legacy `run`/`load-raw` full-manifest path (Section 4) remains snapshot-level, non-incremental, and unchanged.

- Cursor field, data type, precision, timezone, ordering, nullability, inclusive/exclusive filtering, tie-breaking: **Unverified** for a real vendor -- the minimal contract treats `high_water_mark`/`since` as an opaque string this connector never parses or types itself (`pagination.validate_page_envelope` requires only "non-empty string"). **Decision:** the backward-movement guard (below) assumes `high_water_mark` values are lexicographically sortable (e.g. ISO-8601 UTC timestamps, or monotonically increasing opaque tokens) -- a real vendor whose values are not lexicographically orderable would need this section and `cli._run_paginated_load`'s comparison revised together.
- `--since` override: **Verified.** An operator-supplied `--since` overrides the *request* for one extraction (`cli._resolve_since`), but never permanently lowers the durable watermark -- the same backward-movement guard applies uniformly regardless of why a candidate value happens to be behind the currently committed one (see Section 14).
- Multiple candidate watermarks within one run: **Decision.** When a run spans several pages, each may report its own candidate `high_water_mark`; this connector selects the **last page's** value deterministically (`pagination.extract_paginated_run`), because pages are fetched strictly in the order the source's own `next_page_token` chain dictates, so the final page necessarily reflects the most complete traversal of that run's result set.
- Overlap/lookback window: **Unverified** for a real vendor (whether "since `X`" is inclusive/exclusive, or how far back a fresh backfill can safely reach) -- not established anywhere in this repository today.
- Watermark persistence and restart: **Verified.** The durable watermark is `ingest_ops.source_watermarks` (`migrations/005_paginated_extraction_state.sql`), keyed by `(source, endpoint)`, and is only ever advanced transactionally -- in the same commit as the data load, after every reconciliation count matches (`state.commit_watermark`, `cli._run_paginated_load`; see Section 14). It is never advanced during extraction itself. The legacy snapshot-level watermark (`RawSnapshotStore.current_snapshot_id()`) is unchanged and still governs `run`/`load-raw` only.
- Why this captures corrections and late-arriving changes: **Unverified** for a real vendor -- whether "since `X`" reliably re-surfaces a corrected/late-arriving record depends entirely on how the (not-yet-connected) vendor implements its own watermark semantics; this connector has no way to confirm that from the wire contract alone.

## 7. Historical mutability

- **Verified:** previously published snapshots in this connector's own store are immutable. `RawSnapshotStore.finalize()` refuses to overwrite an existing snapshot directory; `check_idempotent_or_conflicting` raises rather than silently accept different content under a reused `snapshot_id` (`extract.py`).
- Whether the upstream source's own underlying records can retroactively change between two snapshots: **Unverified** -- depends on the vendor; the wire contract carries no per-row revision or `updated_at` signal. **Decision:** because full replacement is the extraction strategy, retroactive corrections are captured automatically the next time a new snapshot is extracted -- but only from that point forward. Nothing in this repository detects, diffs, or alerts on what changed between snapshot N and snapshot N+1.
- Re-extraction/lookback requirements: **Decision** -- none beyond "run `extract` again for a fresh snapshot." There is no "re-extract the last N days" concept in this design.

## 8. Corrections, reversals, denials, and deletions

- **Verified:** the wire contract (`manifest.py`, `docs/API_MANIFEST.md`) has no status, reason, event-type, or deletion field of its own. A correction or deletion is represented only implicitly, as a difference between one full snapshot's CSV contents and the next.
- Claim status / denial / reversal columns: **Unverified** whether an eventual vendor's actual CSV rows include claim-status, denial-reason, or adjustment/reversal columns. These would arrive as opaque JSON keys inside `raw_row` (`raw_loader.py` stores every CSV column verbatim, with no fixed schema at the raw layer) and would need explicit mapping in `models/staging/*.sql` to be usable. **Repository-derived assumption:** none of the current `models/staging/*.sql` or `models/final/*.sql` files reference a status/denial/reversal column today; confirm against the real vendor's CSV header once one is connected.
- Replace vs. reference: **Decision.** Corrected records replace earlier ones at the raw-table level (each snapshot `TRUNCATE`s and reloads). Prior snapshots remain on disk under `RAW_DATA_DIR/{source}/{old_snapshot_id}/` until manually pruned (`docs/RUNBOOK.md` "Retention and reruns") but are not queryable via the `raw` schema once superseded.
- Deletions: **Decision/gap.** Represented only as "absence from the next snapshot's CSV" -- a signal visible only by diffing two full snapshots. This repository has no tombstone mechanism and does not currently perform or store such a diff. This is a **blocker** for any downstream consumer that needs true delete-detection, until an explicit diffing or tombstone strategy is designed.
- Downstream idempotency: **Verified.** Reloading the same `snapshot_id` is safe and non-duplicating (`raw_loader.py` `TRUNCATE` + `COPY`; `docs/RUNBOOK.md` "Retention and reruns"). `models/final/*` are dbt tables rebuilt from current raw contents on every run.

## 9. Maximum expected backfill volume

- Expected/maximum date range, estimated records, pages, bytes, or requests: **Unverified -- explicit pre-production capacity-planning blocker.** No vendor is connected and no capacity-planning numbers exist anywhere in this repository (README, RUNBOOK, tests, and fixtures contain only tiny synthetic CSVs under `tests/fixtures/`).
- Known technical ceiling (**Verified**, not a volume estimate): `DEFAULT_MAX_ARTIFACT_BYTES` = 5 GiB per artifact (`api_client.py`) is a hard per-file safety ceiling, not a target or expected size.
- Batching/checkpoint strategy: **Decision.** The only "batching" granularity is the whole snapshot (extract everything, or skip if already published) -- there is no sub-snapshot batching, chunked backfill, or parallel-table-download capability. Artifacts download sequentially (`extract_snapshot`'s `for artifact in manifest.artifacts` loop).
- Operational constraints (runtime, storage, rate-limit impact): **Unverified** -- no load test or benchmark exists in this repository.

## 10. Source timezone and timestamp precision

- Manifest-level `created_at`: **Verified.** Must be ISO-8601 with an explicit offset or `Z` (`manifest.py` normalizes `Z` -> `+00:00`). A naive, offset-less string is currently *accepted* and silently assumed UTC (`created_at.replace(tzinfo=timezone.utc)` when `tzinfo is None`). This silent-UTC assumption is a repository-derived risk worth flagging, not a vendor fact.
- Source CSV row-level timestamp fields (e.g. `claim_start_date`, `paid_date`, `dispensing_date`, `enrollment_start_date`, expected by the Input Layer contract): **Unverified.** Native source timezone, daylight-saving behavior, and precision (seconds/milliseconds/microseconds) are not established anywhere in this repository. These fields are carried as opaque JSON strings inside `raw_row` until `models/staging/*.sql` types them. **Repository-derived assumption:** check each `models/staging/stg_*.sql` cast for the currently-assumed format once a real vendor is connected.
- UTC normalization policy: **Decision.** `_loaded_at` (`raw_loader.py`) is always generated by this connector as `datetime.now(timezone.utc)` -- ingestion-time UTC, never a source-supplied value.
- Date-boundary rules for source-local dates: **Unverified.**

## 11. Identifier stability

- **Verified:** the raw layer keys nothing on a vendor-issued ID. Every row's only structural identifiers are connector-generated: `_snapshot_id`, `_source_row_number` (`raw_loader.py`'s `_RAW_COLUMNS`). Vendor-issued identifiers (`claim_id`, `person_id`, `member_id`, `subscriber_id`, etc.) exist only inside the opaque `raw_row` JSON until staging models extract them.
- Uniqueness scope / composite-key strategy: **Verified** from `models/final/schema.yml` (enforced by `tests/unit/test_input_layer_contract.py`) -- every final model declares exactly one `dbt_utils.unique_combination_of_columns` test that includes `data_source`. This connector's designed idempotent-upsert/dedup key is a composite of business-key columns plus `data_source`, explicitly because a single vendor-issued ID is not trusted to be globally unique across sources.
- Reused, regenerated, mutable, or environment-specific vendor IDs: **Unverified** -- depends on the vendor; not established in this repository today.

## 12. Schema and version-change policy

- Manifest wire-format version: **Verified.** Gated by `SUPPORTED_MANIFEST_VERSIONS` (currently `(1,)`); an unsupported `version` value fails validation loudly (`ManifestError`) before any artifact is downloaded (`manifest.py`).
- Source CSV column layout / vendor field changes: **Unverified** for a real vendor. **Decision:** the raw layer is schema-on-read JSONB (`raw_loader.py`), so an added, renamed, or removed source CSV column does not break extraction or raw loading -- it silently changes what keys exist inside `raw_row`, and is only surfaced when `models/staging/*.sql` fails to find an expected key or a `models/final/*.sql`/`schema.yml` contract test fails (see `tests/unit/test_input_layer_contract.py`'s `_assert_all_columns_present_and_explicitly_cast`). Schema drift is therefore detected downstream in dbt, not at the source-contract layer documented here.
- Unknown fields/enum values: **Decision.** Unknown or extra source CSV columns are silently retained in `raw_row` and unused. Unknown or unexpected values inside a mapped column depend on what each `models/staging/*.sql` cast does with them today (**Unverified** in aggregate -- check each staging model individually).
- Schema-drift detection, alerting, ownership: **Repository-derived.** The closest thing to drift detection is `dbt build` failing on a missing/mistyped `ref()`'d column, plus `tests/unit/test_input_layer_contract.py`'s static checks. There is no automated *source*-side schema-drift monitor (e.g. diffing CSV headers between snapshots) today. No named alerting owner exists -- see "Owner" below.
- Fixture/contract-test update process: **Verified** for the *Tuva package* contract (`README.md` "Upgrading beyond Tuva 0.18.0"). No equivalent documented process exists yet for *source*-CSV schema changes specifically.
- Upgrade/rollback: **Decision.** Same idempotent-snapshot mechanism as any other extraction; rollback means pointing `RAW_DATA_DIR/{source}/current` at a prior `snapshot_id` and reloading it (`make load-raw` against that snapshot), which `docs/RUNBOOK.md` confirms is always safe to rerun.

## 13. PHI classification

**Treated conservatively as containing PHI**, per this task's default policy, independent of which vendor is eventually connected.

- **Verified** from the contract columns this connector is built to populate (`tests/unit/test_input_layer_contract.py`'s `ELIGIBILITY_CONTRACT_COLUMNS` / `MEDICAL_CLAIM_CONTRACT_COLUMNS` / `PHARMACY_CLAIM_CONTRACT_COLUMNS`): direct identifiers and clinical detail including `social_security_number`, `first_name`/`middle_name`/`last_name`, `email`, `phone`, `address`/`city`/`state`/`zip_code`, `birth_date`/`death_date`, up to 25 diagnosis codes and 25 procedure codes per medical claim line, provider NPIs, and payment amounts.
- Logging: **Verified.** This connector's own logging never includes row data -- `logging_utils.py`/`state.py` log only run/table-load metadata, counts, and sanitized error messages, never `raw_row` contents or secrets.
- Fixtures: **Verified.** `tests/fixtures/*.csv` are synthetic (`docs/RUNBOOK.md` "Security notes" states this as an existing repository rule); this document introduces no real data.
- Storage, encryption, access control, retention: **Unverified** at the infrastructure level. This repository does not provision or document disk/volume encryption or PostgreSQL encryption-at-rest, and no retention policy is defined for `RAW_DATA_DIR` or the `raw`/`ingest_ops` schemas. `migrations/003_roles_and_grants.sql` establishes least-privilege database roles (`ingest_role`/`transform_role`), which is access control within Postgres only -- not encryption or retention.
- Redaction: **Decision.** None of the PHI-shaped columns above are redacted anywhere in this pipeline today; both raw JSONB and Input Layer tables carry them in full. Redaction, if required, is a downstream (Tuva package / data mart) or infrastructure concern, not something this connector performs.
- Test-data requirement: **Verified/reinforced.** Tests must continue using synthetic or irreversibly de-identified data only -- this is an existing repository rule, restated here as part of the source contract.

## 14. Reconciliation totals

- Vendor-provided totals: **Verified absent** from both wire contracts. Neither the legacy manifest (`manifest.py`, only `sha256`/`size_bytes` per artifact) nor the paginated envelope (`pagination.py`, only a per-page `record_count`) carries a vendor-supplied *aggregate* record count, claim count, line count, or billed/paid/denied amount total for a whole run.
- Compensating controls actually implemented for the paginated contract (**Verified**, `paginated_loader.py`/`cli._run_paginated_load`), all three enforced as part of one transactional load, any mismatch failing the whole run:
  1. Each page's `metadata.record_count` is checked against its actual decompressed JSONL line count, independently re-verified at load time (`paginated_loader.verify_run_manifest`) -- not just trusted from the manifest written at extract time.
  2. The sum of every page's `record_count` is checked against the run manifest's own `total_record_count` (`verify_run_manifest`).
  3. The number of raw rows actually present in the database for the run (`paginated_loader.loaded_row_count`, a fresh `COUNT(*) WHERE _snapshot_id = run_id` -- correct whether this is the first load or an idempotent repeat) is checked against that same `total_record_count` (`cli._run_paginated_load`).
  Any of the three mismatching raises `ReconciliationError`, rolls back the whole transaction, and never commits the watermark (Section 6) -- see `run_failed`/`reconciliation_completed` structured log events.
- Compensating controls for the legacy full-manifest contract (**Verified**, unchanged): per-artifact SHA-256/byte-count verification (`ApiClient.download_artifact`, `raw_loader.verify_file_checksum`) and per-table row counts (`ingest_ops.table_loads.row_count`, `state.table_load_row_counts()`).
- Comparison tolerances, frequency, alert thresholds, investigation procedure: **Unverified.** None are defined in this repository for either contract; counts are recorded/reconciled per run but nothing currently compares them run-over-run or alerts on an unexpected swing.
- **Blocking note:** without vendor-provided *business* totals (as opposed to this connector's own structural/technical reconciliation above), whether "did we receive every claim the payer sent this period" holds cannot be confirmed by this connector alone -- only structural/technical completeness (checksums, per-page/per-run/per-database record counts) is currently verifiable.


## 15. Object storage

This section documents the object-storage-backed ingestion path added
alongside the wire/pagination contract above (Sections 1-14, unchanged).
It extends the same endpoint-scoped `extract`/`load`/`sync` workflow
(`--storage object-storage`, see `cli.py`) rather than replacing it or
standing up an unrelated parallel pipeline -- the `--endpoint` vocabulary
(`endpoints.py`), page request/response envelope
(`pagination.validate_page_envelope`), and `ApiClient` are all reused
unchanged.

### Object storage as the durable source of truth

Every published run's pages, manifest, and success marker are immutable
objects in object storage (`OBJECT_STORAGE_PROVIDER=local` for
development, using `object_storage.local.LocalFilesystemBackend`;
`OBJECT_STORAGE_PROVIDER=s3` in production, using
`object_storage.s3.S3Backend` against real AWS S3 or any S3-compatible
endpoint such as MinIO). This is the durable, replayable source of
truth -- PostgreSQL's raw tables (Section "Raw metadata definitions"
below) are a convenience layer derived FROM object storage by
`object_raw_loader.py`, never the other way around. A raw table can
always be dropped and fully rebuilt by re-running `load`/`sync` against
every previously published run_id; object storage cannot be
reconstructed from PostgreSQL (raw payloads that fail loading are
represented in PostgreSQL only as a `rejected_record` pointer back to
the object, never a full copy -- see "Rejected-record investigation").

### Object-key convention

    <prefix>/vendor=<vendor>/endpoint=<endpoint>/load_date=<YYYY-MM-DD>/run_id=<uuid>/page=<NNNNNN>.jsonl.gz

Example:

    raw/vendor=acme/endpoint=medical_claim/load_date=2026-08-14/run_id=550e8400-e29b-41d4-a716-446655440000/page=000001.jsonl.gz

- `prefix` -- configurable (`OBJECT_STORAGE_PREFIX`, default `"raw"`).
- `vendor` -- `SOURCE_NAME` (`config.py`), validated as a safe object-key
  path segment (`object_storage/keys.py`).
- `endpoint` -- the STABLE, normalized snake_case partition name
  (`medical_claim`, `pharmacy_claim`, `eligibility`), never the
  hyphenated `--endpoint` CLI form, via `endpoints.table_for_endpoint`
  (the same single authoritative mapping every other part of this
  connector already uses -- never re-derived).
- `load_date` -- the UTC calendar date extraction started on
  (`object_storage.keys.utc_load_date`, always computed from a
  timezone-aware UTC instant).
- `run_id` -- a true, randomly generated UUID4 (`object_storage.keys.new_run_id`)
  -- never a timestamp- or endpoint-derived value (those are already
  separate key components; embedding them again in `run_id` would make
  it a leaky composite key instead of an opaque identifier).
- `page` -- a six-digit, 1-based page number (`page=000001` ... `page=999999`).

Each page is stored as gzip-compressed JSONL, one exact source record
per line, values/structure unchanged from what the source sent (only
each record's own top-level key order is normalized for storage
determinism -- see `object_storage.publish.gzip_jsonl`).

### Publication and success-marker semantics

Publication order is fixed and never relies on filesystem rename
semantics for atomicity (`object_storage.publish.RunPublisher`):

1. Every page object is written first (in any order -- pages are
   independent immutable objects).
2. The run manifest is written next -- object keys, page numbers,
   compressed byte sizes, SHA-256 checksums, record counts, cursor
   metadata (`requested_cursor`/`candidate_cursor`), and extraction
   timestamps for every page.
3. The success marker (`_SUCCESS`) is published LAST, only once every
   page and the manifest are already durable. Its content is the
   manifest's own SHA-256, so `verify.py` can cheaply confirm the
   marker actually corresponds to the manifest it references.

**A run without a valid success marker and manifest is never loadable**
(`object_storage.verify.load_and_verify_manifest` raises
`RunNotPublishedError`) -- regardless of how many of its pages happen to
be present.

**Immutability:** writing the exact same bytes to an already-published
key is a safe no-op (retry-safe); writing DIFFERENT bytes to an
already-published key raises `ImmutableObjectError`
(`object_storage.base.StorageBackend.put`'s contract, upheld identically
by every backend). `object_storage.s3.S3Backend` additionally attempts a
conditional (`IfNoneMatch: "*"`) `PutObject` so two concurrent publishers
racing to write the same key can never have the second writer silently
clobber the first -- see that module's docstring for its documented,
inherent race-window limitation on S3-compatible services that do not
support conditional writes.

### Replay instructions

Loading/replaying a run ALWAYS reads from object storage, never from a
local directory alone (`object_raw_loader.load_verified_run` calls
`object_storage.verify.load_and_verify_manifest`, which re-verifies
existence, checksum, gzip integrity, and JSONL record count for every
page before anything is loaded). To replay a specific run:

    tuva-ingest load --storage object-storage --run-id <run_id>

This is safe to repeat for the same `run_id` (inserts zero new rows on
retry -- see "The stable uniqueness rule" below) and safe to run for a
DIFFERENT `run_id` that happens to contain the same logical source rows
(also zero new rows, by the same uniqueness rule) -- see
`tests/integration/test_object_storage_pipeline_integration.py`'s
`TestCopyAndMerge`.

### Raw metadata definitions

Every row `object_raw_loader.py` writes carries exactly these seven
columns (added by `migrations/006_object_storage_raw_contract.sql`,
nullable so they coexist with the legacy loader's own four columns on
the same physical tables):

| Column | Type | Meaning |
| --- | --- | --- |
| `_ingestion_run_id` | `uuid` | The run that loaded this row. |
| `_ingested_at` | `timestamptz` | When this row was loaded. |
| `_source_endpoint` | `text` | The normalized snake_case endpoint. |
| `_source_record_id` | `text` | See "Source-record-id contract" below. |
| `_source_updated_at` | `timestamptz` | See "Source-updated-at contract" below. |
| `_payload_hash` | `text` | Lowercase SHA-256, see "Payload hash" below. |
| `_raw_payload` | `jsonb` | The minimally interpreted complete source object. |

**Legacy compatibility:** the pre-existing `raw_row` column (populated
only by the legacy CSV loader, `raw_loader.py`) is left completely
unchanged -- it is a SEPARATE column, never renamed or shared, so the
two loaders can never independently populate (and silently drift) the
same column. dbt staging models bridge the two explicitly via
`coalesce(_raw_payload, raw_row)` (see "Legacy workflow compatibility"
below) -- this is the one, explicit place the two payload columns are
ever reconciled.

### Source-record-id contract

Centralized in `endpoint_contract.py` (`ENDPOINT_ID_FIELDS`), never
re-derived per call site:

| Endpoint | Source field(s) |
| --- | --- |
| `eligibility` | `person_id` |
| `medical_claim` | `claim_id` + `claim_line_number` |
| `pharmacy_claim` | `claim_id` + `claim_line_number` |

A composite id is encoded via `endpoint_contract.encode_composite_id` --
a length-prefixed encoding (`"<byte-length>:<value>..."` per part),
never naive delimiter concatenation, so a value that happens to contain
what would otherwise look like a delimiter can never collide with a
different logical id (`("ab", "c")` and `("a", "bc")` always encode
differently -- see `tests/unit/test_endpoint_contract.py`).

A record missing (or with a blank/non-scalar) required id field is
rejected with reason code `missing_source_id` -- never silently
defaulted or dropped without a trace.

### Source-updated-at contract

`_source_updated_at` is parsed from the endpoint's explicit source
update-timestamp field -- `updated_at` for every currently-registered
endpoint (`endpoint_contract.ENDPOINT_TIMESTAMP_FIELD`; a future
endpoint whose verified contract establishes a different field name
would add an entry here). **Ingestion time is never substituted** when
this field is missing or does not parse as ISO-8601 -- both cases are
always rejected (`missing_source_timestamp` / `invalid_source_timestamp`
reason codes), never defaulted.

### Payload hash

`_payload_hash` is the lowercase hex SHA-256 of
`endpoint_contract.canonical_json_bytes`: the complete source record
serialized with sorted keys and compact separators (`json.dumps(...,
sort_keys=True, separators=(",", ":"))`), UTF-8 encoded. The same
logical JSON object hashes identically regardless of input key order,
at every nesting level (`tests/unit/test_endpoint_contract.py`).

### The stable uniqueness rule

A partial unique index on every raw table enforces:

    (_source_endpoint, _source_record_id, _source_updated_at, _payload_hash)
    WHERE _source_record_id IS NOT NULL

Named `<table>_source_stable_uk` (`migrations/006_object_storage_raw_contract.sql`).
Scoped to `_source_record_id IS NOT NULL` so it never applies to (and can
never conflict with) legacy-loaded rows, which always leave
`_source_record_id` NULL.

**Why the payload hash is included:** this key deliberately permits a
corrected record with the SAME source id and update timestamp when its
payload hash differs (the source corrected a field without bumping its
own `updated_at`) -- both versions are retained as separate rows, never
silently overwritten -- while still suppressing an EXACT replay of the
same logical row (identical id, timestamp, AND hash) as a duplicate. A
changed `_source_updated_at` for the same id is likewise always retained
as a new version, never merged/overwritten.

### COPY-to-temp and transactional merge

`object_raw_loader.load_verified_run`, page by page (a run is never
loaded fully into memory):

1. Classifies each record (`endpoint_contract.derive_source_record_id`/
   `derive_source_updated_at` -- never raises for a data-quality
   problem; one bad record never aborts the rest of a page/run).
2. Bulk-loads accepted rows into a transaction-local `TEMP TABLE` via
   `psycopg`'s `COPY FROM STDIN`.
3. Merges from the temp table into the permanent raw table via
   `INSERT ... SELECT ... ON CONFLICT (...) WHERE _source_record_id IS
   NOT NULL DO NOTHING` against the uniqueness rule above.
4. Reconciles, per page: `source records == accepted + rejected` and
   `accepted == inserted + exact duplicates` (`ReconciliationError` on
   any mismatch).
5. Writes `ingestion_page`, `rejected_record`, and `schema_observation`
   rows for that page -- all inside the SAME transaction, never
   committed individually.

Only after every page succeeds does the loader lock and validate the
cursor (see below) and mark the run `committed`. **One single
`conn.commit()`** (owned by the caller, `cli._run_object_load` --
`object_raw_loader.py`/`state.py`'s "canonical object-storage-backed
operational model" functions never call `commit()`/`rollback()`
themselves) makes the raw merge, every operational write, and the
cursor advance all become visible together -- or, on any failure, the
caller's `conn.rollback()` discards all of them together, followed by a
SEPARATE, freshly-committed `state.mark_run_failed` write that preserves
the sanitized failure reason without ever re-entering the rolled-back
transaction.

### Cursor transaction and concurrency behavior

`ops.ingestion_cursor` (keyed by `(vendor, endpoint)`) is the SOLE
canonical cursor source for the object-storage-backed workflow -- never
`ops.source_watermarks` (which remains, unchanged, the cursor store for
the legacy local-filesystem paginated workflow; see "Legacy workflow
compatibility"). Guarantees:

- Extraction never advances the cursor (`object_extract.py` only writes
  `ingestion_run.candidate_cursor`, never `ingestion_cursor`).
- Object publication alone (reaching a durable `_SUCCESS` marker) never
  advances the cursor either.
- A verified PostgreSQL commit is the ONLY event that advances the
  cursor (`state.commit_cursor`, called once, as close to the end of the
  transaction as possible, immediately before `state.mark_run_committed`).
- `state.lock_cursor_for_update` takes `SELECT ... FOR UPDATE` on the
  row (creating it first at NULL/0 if this is the endpoint's first-ever
  load) so a second concurrent run for the same `(vendor, endpoint)`
  blocks until the first transaction commits or rolls back -- it can
  never silently race past it.
- Backward cursor movement is refused (`CursorError`) if the candidate
  cursor is lexicographically less than the already-committed cursor.
- `lock_version` (optimistic-concurrency metadata) is checked by
  `commit_cursor`'s `UPDATE ... WHERE lock_version = %s`; because the row
  lock above is already held for the whole transaction, a mismatch here
  can only happen from a caller bug, not a legitimate race -- it raises
  `CursorError` rather than silently overwriting a newer cursor.

### Six-schema lineage

| Schema (default name) | Owner | Purpose |
| --- | --- | --- |
| `ops` | This connector | Pipeline state and audit records (`ingestion_run`/`ingestion_page`/`ingestion_cursor`/`rejected_record`/`schema_observation`, plus the legacy `ingestion_runs`/`table_loads`/`source_watermarks`). |
| `raw_incoming` | This connector | Minimally interpreted source data (both loaders' raw tables). |
| `staging_incoming` | dbt (this project's own models) | Cast, normalize, and deduplicate (`models/staging/*.sql`). |
| `input_layer` | dbt (this project's own models) | The Tuva Input Layer contract (`models/final/*.sql`). |
| `analytics_core` | dbt (the pinned Tuva package) | Tuva's own core data model + terminology output. |
| `analytics_marts` | dbt (the pinned Tuva package) | Tuva's own mart outputs. |

All six are independently configurable
(`RAW_SCHEMA`/`OPS_SCHEMA`/`STAGING_SCHEMA`/`INPUT_LAYER_SCHEMA`/
`ANALYTICS_CORE_SCHEMA`/`ANALYTICS_MARTS_SCHEMA`, validated via the
existing shared identifier policy, `identifiers.py`) and must be
pairwise distinct (`config.py`'s cross-field validator). **The default
values for `RAW_SCHEMA`/`OPS_SCHEMA` changed** (from `raw`/`ingest_ops`
to `raw_incoming`/`ops`) as part of this change -- see
`docs/RUNBOOK.md` "Upgrade notes" for what an existing deployment must
do. `analytics_core`/`analytics_marts` routing for the pinned Tuva
package's own models is implemented in
`macros/generate_schema_name.sql` -- see that macro's own docstring for
its exact heuristic and its documented "best-effort, unverified without
`dbt deps`" limitation (this sandboxed environment could not run `dbt
deps` to confirm the real package's exact schema-name literals; verify
with `dbt list` once you have network access).

### Schema observation behavior and limitations

`schema_observation.py` walks each decoded JSON record recursively and
records only field PATHS and JSON TYPE NAMES -- never values (no PHI is
ever stored in `schema_observation`). Behavior:

- Nested objects produce dotted paths (`address.city`).
- Arrays produce the array path itself (type `"array"`) plus each
  element's own path with a trailing `"[]"` segment -- an array's
  elements contribute their field paths ONCE, never once per index, so
  observations are independent of array length.
- `null` is recorded as its own distinct type (`"null"`), never merged
  away or skipped -- a field that is sometimes null and sometimes a
  string legitimately accumulates BOTH types at that path; that IS the
  drift signal this table exists to surface.
- A field observed with more than one type across records/runs simply
  accumulates more than one type at that path -- this module never
  "picks a winner"; a human reviewing `schema_observation` does, if
  ever needed.
- The fingerprint (`schema_observation.fingerprint`) is a SHA-256 over
  the sorted `(path, type)` representation -- independent of dict/set
  iteration order, sensitive to any real shape change (proven in
  `tests/unit/test_schema_observation.py`).
- **Limitation:** this is per-run/per-page shape drift detection only --
  it does not detect a value-level anomaly (e.g. a string field that
  starts containing dates), and it does not alert; `schema_observation`
  must be queried/compared manually or by a downstream tool.

### Rejected-record investigation

A rejected record's `ops.rejected_record` row NEVER carries the raw
payload or any source field value -- only a reason code, a sanitized
(PHI-free) detail string, the derived `source_record_id` when safely
available, `payload_hash`, and `raw_object_key` (a durable pointer back
to the exact, immutable page object in object storage). To investigate:

1. `SELECT reason_code, detail, raw_object_key, rejected_at FROM
   ops.rejected_record WHERE run_id = '<run_id>' ORDER BY page_number,
   record_position;`
2. Fetch `raw_object_key` from object storage directly (e.g.
   `object_storage_backend.get(raw_object_key)`, or the equivalent `aws
   s3 cp`/`mc cat` command against the configured bucket) and decompress
   (gzip) to locate the exact record at `record_position` within that
   page's JSONL.

`ops.rejected_record` is PHI-bearing (a `raw_object_key` is a pointer to
PHI): `transform_role` (dbt) has NO grant on it whatsoever
(`migrations/006_object_storage_raw_contract.sql`), and PUBLIC is
explicitly revoked.

### Legacy workflow compatibility

| Workflow | Status | Notes |
| --- | --- | --- |
| `load-raw`/`run` (legacy full-manifest CSV) | **Fully compatible, unchanged.** | `raw_loader.py` untouched; `raw_row`/`_snapshot_id`/etc. columns untouched; `ingestion_runs`/`table_loads` untouched. |
| `extract`/`load`/`sync --storage filesystem` (local-filesystem paginated) | **Fully compatible, unchanged.** | `pagination.py`/`paginated_loader.py` untouched; `ops.source_watermarks` remains its sole cursor source. |
| `extract`/`load`/`sync --storage object-storage` (this change) | **New, additive.** | Opt-in via `--storage object-storage`; default remains `filesystem`. |
| dbt staging models | **Compatible via explicit coalesce.** | `coalesce(_raw_payload, raw_row)` -- see "Raw metadata definitions" above; both loaders' rows normalize identically. |
| `RAW_SCHEMA`/`OPS_SCHEMA` overrides | **Compatible.** | An explicit override continues to work exactly as before; only the DEFAULT value changed -- see `docs/RUNBOOK.md` "Upgrade notes". |

## Readiness

Extraction **may proceed** for the generic, already-implemented mechanisms themselves -- both the paginated JSON envelope/watermark/reconciliation contract (Sections 1-4, 6, 14; `secrets.py`, `pagination.py`, `paginated_loader.py`, `state.get_watermark`/`commit_watermark`) and the legacy manifest/snapshot mechanism (`api_client.py`, `manifest.py`, `extract.py`, `raw_loader.py`), plus the retry policy (Section 5), the composite dedup-key strategy (Section 11), and the conservative PHI handling (Section 13), are already built and tested (`tests/unit/`, `tests/integration/`).

Extraction against a **real, specific external vendor is blocked** until all of the following are resolved for that vendor:

1. Its actual base URL, authentication flow (confirm static bearer token vs. OAuth/other; confirm the secret-manager provider and secret shape it will actually be issued through), and published rate limits are confirmed (Sections 1, 2, 5).
2. For the paginated contract: its exact page-request/response envelope field names, page-token/watermark semantics (ordering, expiry, inclusive/exclusive `since` filtering), and typical page/backfill volume are confirmed to match Sections 4, 6, 9 -- or this connector's minimal contract, `pagination.py`, and `docs/API_MANIFEST.md`/this document are revised together first. For the legacy contract: its actual CSV column layout is confirmed to supply the fields the Input Layer contract requires, and whether it carries claim-status/denial/reversal columns (Sections 3, 8).
3. PHI storage, encryption, access-control, and retention controls are put in place for `RAW_DATA_DIR` and the `raw`/`ingest_ops` schemas beyond database role grants -- currently unverified/absent (Section 13).
4. A deletion/tombstone or snapshot-diff strategy is decided if any downstream consumer needs delete-detection (Section 8).
5. Backfill volume is estimated and a batching/runtime plan is confirmed to fit operational constraints -- currently entirely unverified (Section 9).
6. Reconciliation tolerances and alerting are defined even in the absence of vendor-provided totals (Section 14).

Until items 1-6 are resolved for a specific vendor, this connector must only be pointed at a contract-compliant test or mock server (as already done in `tests/unit/` and `tests/integration/`), never at a live vendor endpoint carrying real PHI.

## Owner

This repository has no `CODEOWNERS` file or other documented team-ownership convention as of this writing (confirmed by repository search). An owner or responsible team should be assigned before this connector is pointed at a real vendor; until then, changes to this document and the extraction code it governs should be reviewed the same way any other change to `src/tuva_ingest/` is reviewed.

## Last verified

2026-08-17, against the state of this repository after adding the object-storage-backed ingestion path (`src/tuva_ingest/object_storage/`, `object_extract.py`, `object_raw_loader.py`, `endpoint_contract.py`, `schema_observation.py`, `migrations/006_object_storage_raw_contract.sql`). Re-verify this document (and update this date) whenever `src/tuva_ingest/{config,api_client,manifest,extract,raw_loader,state,secrets,pagination,paginated_loader,object_storage/,object_extract,object_raw_loader,endpoint_contract,schema_observation}.py`, `docs/API_MANIFEST.md`, or the connected upstream source changes.
