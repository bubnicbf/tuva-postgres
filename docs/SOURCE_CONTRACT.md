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
and `migrations/007_object_storage_raw_contract.sql`/`008_operational_table_hardening.sql` for the object-storage-
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
- Environment-specific URLs: **Unverified.** No sandbox/production URL convention exists in the repository. `PIPELINE_ENVIRONMENT` (`config.py`) only labels operational metadata (`<OPS_SCHEMA>.ingestion_runs.environment`, default schema name `ops`); it never selects a URL.
- API version and how it is selected: **Verified.** The manifest's wire-format version is the `version` field inside the fetched JSON body itself, checked against `SUPPORTED_MANIFEST_VERSIONS = (1,)` (`manifest.py`). There is no URL path segment (e.g. `/v1/`) or version header.
- Version deprecation: **Unverified.** No deprecation policy exists for manifest version 1. Introducing version 2 requires a deliberate, reviewed update to `SUPPORTED_MANIFEST_VERSIONS` (see `docs/RUNBOOK.md` "Upgrading" for the analogous single-commit convention used for the pinned Tuva package).

## 2. Authentication

- Mechanism: **Verified, two supported modes.** (1) Static bearer token, `Authorization: Bearer {TUVA_API_TOKEN}` (or the secret-provider-resolved equivalent) -- the default, and the only mode the legacy `run`/`load-raw` commands use. (2) OAuth 2.0 client-credentials grant, selected by setting `TUVA_OAUTH_TOKEN_URL` (`src/tuva_ingest/oauth.py`'s `OAuthTokenManager`) -- available to the paginated `extract`/`sync` commands only, entirely additive/optional. Either way, exactly one resolved access token is sent as `Authorization: Bearer {token}` on every request (`ApiClient._headers()`, `api_client.py`).
- Required headers: **Verified.** `Authorization`, `User-Agent: tuva-ingest/{__version__}`, `Accept: application/json, text/csv;q=0.9, */*;q=0.1` (`api_client.py`).
- Scopes, token endpoint, signing rules: **Repository-derived assumption** for the OAuth mode. No vendor-documented grant type exists anywhere in this repository -- OAuth 2.0 client-credentials (`TUVA_OAUTH_TOKEN_URL`/`TUVA_OAUTH_CLIENT_ID`/`TUVA_OAUTH_CLIENT_SECRET`/`TUVA_OAUTH_SCOPES`) was chosen as the most common/conservative default for a machine-to-machine integration; `refresh_token` rotation is supported opportunistically if the token endpoint returns one, but is not assumed required. A vendor requiring a different grant type (authorization-code, JWT-bearer, mTLS, request signing) is **not yet supported** and would require `oauth.py` to be revised. No request-signing scheme (e.g. HMAC) exists in the code for either auth mode.
- Token lifetime: **Verified for the connector's own handling, Unverified for a real vendor's actual values.** `OAuthTokenManager` reads `expires_in` from the token response and tracks a monotonic expiry deadline, refreshing proactively once `TUVA_OAUTH_REFRESH_SKEW_SECONDS` (default 60) of lifetime remains; it never returns an already-expired token. What lifetime/rotation cadence a real vendor's token endpoint actually issues is unconfirmed.
- On an unexpected `401` from the source API (OAuth mode only): **Decision.** `ApiClient` forces exactly one token refresh and replays the request exactly once (`_request_with_retries`) -- authentication recovery, not a general retry, and never looped. A `401` on the replay fails immediately.
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
- Termination condition: **Verified.** `metadata.next_page_token` being null or absent. Two independent hard safety ceilings additionally bound any run that never terminates cleanly: `TUVA_API_MAX_PAGES` (default 10,000 pages) and `TUVA_API_MAX_RECORDS_PER_RUN` (default 2,000,000 records, counting every source record including ones later quarantined -- checked after envelope validation but before publishing each page). Every requested/returned page token is tracked for the run's lifetime, so a repeated request token, a repeated `next_page_token`, or the server echoing the current token back as the next one are all treated as a pagination cycle and fail immediately (`PaginationError`) rather than looping forever (`pagination.py`); hitting exactly a limit succeeds, one page or record past it fails the run with nothing partial published and the watermark left untouched.
- Ordering guarantees: **Unverified.** The paginated contract's within-page record order, and whether pages themselves are guaranteed non-overlapping/gap-free by any real, eventually-connected vendor, are not established anywhere in this repository -- this connector requests pages strictly in the order the source's own `next_page_token` chain dictates and never reorders or re-sorts records itself.
- Token expiry / safe restart: **Unverified** for a real vendor (whether a `page_token`/`next_page_token` value expires, and after how long). This connector holds no long-lived pagination state across process restarts -- a killed/restarted `extract` simply starts a fresh run from page 1 using the current watermark; a `load`/`sync` failure never leaves an inconsistent watermark (Section 14 below; `state.get_watermark`/`commit_watermark`).
- Duplicate/missing records across boundaries: **Decision.** Loading is idempotent per run (`_snapshot_id`, `_source_row_number` unique index -- `migrations/005_paginated_extraction_state.sql`), so repeating a load never duplicates rows within one run; whether a real vendor's own pagination can itself produce duplicate or missing records across two different runs' page boundaries (e.g. under concurrent writes on the source side) is **Unverified**, restated in Readiness.
- **Decision, restated in Readiness:** the specific real vendor eventually connected must be confirmed to implement this exact minimal envelope (or this section, `pagination.py`, and `docs/API_MANIFEST.md` must be revised together) before extraction against that vendor is considered ready.

## 5. Rate limits and retry behavior

- Published or observed vendor limits: **Unverified.** No specific vendor is connected; nothing in this repository documents an actual published rate limit.
- Client-side retry policy (**Verified**, `src/tuva_ingest/retry.py`'s `BoundedRetryExecutor` -- one shared policy used identically by `api_client.py`'s source-API requests and `oauth.py`'s token-endpoint requests):
  - Retryable: `429`, `502`, `503`, `504`, and httpx connection/timeout errors (`RETRYABLE_STATUS`). **Decision, changed from an earlier draft of this policy:** `500` is deliberately *not* retryable by default -- only the server statuses this connector's own contract explicitly treats as transient. Every other 4xx (`400`, `401` outside the single-shot OAuth recovery above, `403`, `404`, `409`, `422`, etc.), and any application-level failure (envelope validation, cycle detection, checksum mismatch), fails immediately and is never retried.
  - `Retry-After`: both the numeric-seconds and HTTP-date forms are supported (`parse_retry_after`); an HTTP-date is resolved relative to current wall-clock time (a past date yields zero delay); a negative, malformed, non-finite, or unreasonably large value (`MAX_REASONABLE_RETRY_AFTER_SECONDS`) is rejected and falls back to exponential backoff rather than being honored as-is.
  - Backoff: full-jitter exponential (`random() * min(TUVA_API_MAX_RETRY_DELAY_SECONDS, base * 2**attempt)`), fully injectable (clock/random/sleep) for deterministic unit tests.
  - Bounds: **two independent limits**, either of which stops retrying -- `TUVA_API_MAX_RETRIES` (attempt count, default `5`) and `TUVA_API_MAX_RETRY_DURATION_SECONDS` (total elapsed time via a monotonic clock, default `120`). If honoring the next delay (backoff or `Retry-After`) would exceed the remaining duration budget, the request fails immediately rather than oversleeping past the deadline. Applied per logical request (one page request, one manifest fetch, one artifact download, or one token-endpoint call), not per whole extraction run.
  - Non-retryable errors: `401`/`403` raise immediately with a message that never includes the token; `404` raises immediately as "not found"; any other non-2xx, non-retryable status raises immediately. A failed response is always closed/released before any retry sleep.
- Response headers consulted: **Verified** -- only `Retry-After`. No `X-RateLimit-*`/`RateLimit-*` headers are read anywhere in the client.
- **Decision:** this policy is generic and defensive by design, not tuned to a specific vendor's published limits. Whether it provides sufficient headroom for a real vendor is **Unverified** until one is connected and load-tested.

## 6. Incremental extraction field

**Verified:** the paginated `extract`/`load`/`sync` path performs true incremental extraction, keyed by an opaque, source-supplied **high-water mark** -- not a connector-chosen field name/column. Every page response's `metadata.high_water_mark` is a candidate value for the *next* incremental run; this connector never inspects, types, or filters by any specific row-level field itself (e.g. no hardcoded `updated_at` column) -- the source alone decides what "since `X`" means and returns accordingly (`pagination.py`, `docs/SOURCE_CONTRACT.md` Section 4). The legacy `run`/`load-raw` full-manifest path (Section 4) remains snapshot-level, non-incremental, and unchanged.

- Cursor field, data type, precision, timezone, ordering, nullability, inclusive/exclusive filtering, tie-breaking: **Unverified** for a real vendor -- the minimal contract treats `high_water_mark`/`since` as an opaque string this connector never parses or types itself (`pagination.validate_page_envelope` requires only "non-empty string"). **Decision:** the backward-movement guard (below) assumes `high_water_mark` values are lexicographically sortable (e.g. ISO-8601 UTC timestamps, or monotonically increasing opaque tokens) -- a real vendor whose values are not lexicographically orderable would need this section and `cli._run_paginated_load`'s comparison revised together.
- `--since` override: **Verified.** An operator-supplied `--since` overrides the *request* for one extraction (`cli._resolve_since`), but never permanently lowers the durable watermark -- the same backward-movement guard applies uniformly regardless of why a candidate value happens to be behind the currently committed one (see Section 14).
- Multiple candidate watermarks within one run: **Decision.** When a run spans several pages, each may report its own candidate `high_water_mark`; this connector selects the **last page's** value deterministically (`pagination.extract_paginated_run`), because pages are fetched strictly in the order the source's own `next_page_token` chain dictates, so the final page necessarily reflects the most complete traversal of that run's result set.
- Overlap/lookback window: **Unverified** for a real vendor (whether "since `X`" is inclusive/exclusive, or how far back a fresh backfill can safely reach) -- not established anywhere in this repository today.
- Watermark persistence and restart: **Verified.** The durable watermark is `<OPS_SCHEMA>.source_watermarks` (default schema name `ops`; `migrations/005_paginated_extraction_state.sql`), keyed by `(source, endpoint)`, and is only ever advanced transactionally -- in the same commit as the data load, after every reconciliation count matches (`state.commit_watermark`, `cli._run_paginated_load`; see Section 14). It is never advanced during extraction itself. The legacy snapshot-level watermark (`RawSnapshotStore.current_snapshot_id()`) is unchanged and still governs `run`/`load-raw` only.
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
- Compensating controls for the legacy full-manifest contract (**Verified**, unchanged): per-artifact SHA-256/byte-count verification (`ApiClient.download_artifact`, `raw_loader.verify_file_checksum`) and per-table row counts (`<OPS_SCHEMA>.table_loads.row_count`, default schema name `ops`; `state.table_load_row_counts()`).
- Comparison tolerances, frequency, alert thresholds, investigation procedure: **Unverified.** None are defined in this repository for either contract; counts are recorded/reconciled per run but nothing currently compares them run-over-run or alerts on an unexpected swing.
- **Blocking note:** without vendor-provided *business* totals (as opposed to this connector's own structural/technical reconciliation above), whether "did we receive every claim the payer sent this period" holds cannot be confirmed by this connector alone -- only structural/technical completeness (checksums, per-page/per-run/per-database record counts) is currently verifiable.

## 15. Structural record validation and quarantine

- Mechanism: **Verified**, `src/tuva_ingest/validators.py`, applied at **load** time (extraction remains an unmodified, byte-for-byte mirror of what the source sent -- see Section 4). Every record is checked against a narrow, structural-only contract derived directly from Section 3's documented record grain: `eligibility` requires a non-blank string `person_id`; `medical_claim`/`pharmacy_claim` require a non-blank string `claim_id` and a scalar `claim_line_number`; any populated known date field must be recognizably date-shaped. **Decision:** this connector never invents a clinical/business rule here, and a null/absent *optional* field is never grounds for quarantine.
- Reason codes: **Decision**, a fixed allowlist (`record_not_object`, `missing_required_field`, `invalid_required_type`, `invalid_identifier`, `invalid_date_format`, `schema_validation_failed`), enforced both in application code and by a database `CHECK` constraint (`migrations/006_record_quarantine.sql`).
- Storage and access: **Verified.** A structurally invalid record is written to `<OPS_SCHEMA>.quarantined_records` (default schema name `ops`; PHI-bearing) and is never also loaded into its endpoint's raw table. Access is more restrictive than the raw schema: `PUBLIC` and `TRANSFORM_ROLE` have no access at all; `INGEST_ROLE` is granted `INSERT` only, never `SELECT`/`UPDATE`/`DELETE`. No operational "quarantine review" role exists in this repository's role model yet -- see `docs/RUNBOOK.md` "Quarantined records".
- Reconciliation interaction: **Verified**, extends Section 14's three-way check to a required identity: `source_record_count == raw_loaded_count + quarantined_count`. `quarantined_count` is computed from the connector's own deterministic, idempotent classification pass over the immutable page files on every call -- never a `SELECT` against the quarantine table, consistent with its INSERT-only grant.
- What this does *not* cover: **Unverified/out of scope.** Clinical or business-rule validity (a syntactically valid but clinically implausible diagnosis code, an out-of-range paid amount, a claim referencing a member not present in eligibility) is not checked here -- that remains a downstream (dbt staging/DQ) concern, unchanged by this section.

## Operational tables (control plane)

The five canonical operational/control tables the object-storage-backed
workflow (`object_extract.py`/`object_raw_loader.py`) writes to --
`ingestion_run`, `ingestion_page`, `ingestion_cursor`, `rejected_record`,
`schema_observation` -- all live in the configurable `OPS_SCHEMA`
(default `ops`; **Decision:** never hard-coded -- every reference goes
through `db.qualified_relation`/`identifiers.validate_identifier`, see
"Application implementation" in `docs/RUNBOOK.md`). They are **control-
plane objects, never Tuva Input Layer models**: `migrations/007_object_storage_raw_contract.sql`/
`008_operational_table_hardening.sql` create them with plain SQL, never
dbt; `models/sources.yml` has no entry for `OPS_SCHEMA` at all (dbt only
ever reads `RAW_SCHEMA`); and no dbt model may read `rejected_record` or
any other operational table as a substitute for source data. See
`migrations/007_object_storage_raw_contract.sql`/`008_operational_table_hardening.sql`
for the exact column-level DDL this section summarizes; both are
**Verified** directly from those files (which are also what
`tests/unit/test_migrations.py`'s `TestMigration007ObjectStorageRawContract`/
`TestMigration008OperationalTableHardening` structurally check).

**Legacy plural tables, still present and unambiguous:** `ingestion_runs`/
`table_loads` (`migrations/002_ingestion_control.sql`, extended by 004)
and `source_watermarks` (`migrations/005_paginated_extraction_state.sql`)
back the legacy CSV/full-manifest workflow (`raw_loader.py`) and the
local-filesystem paginated workflow (`pagination.py`/`paginated_loader.py`)
respectively; `quarantined_records` (`migrations/006_record_quarantine.sql`)
backs that same local-filesystem workflow's structural-validation
quarantine. **None of these four legacy tables is ever written to by the
object-storage-backed workflow**, and the five canonical singular tables
above are never written to by the legacy/local-filesystem workflows --
`object_raw_loader.py`/`state.py`'s "canonical object-storage-backed
operational model" section only ever touches the five singular tables;
`raw_loader.py`/`paginated_loader.py`/`quarantine.py` only ever touch the
four legacy ones. This separation is enforced by convention (each
module's own `state.py` functions target one fixed set of table names --
see `state.py`'s own module docstring, "Two different commit
disciplines"), not by a database-level constraint; do not add a new
call site that writes a legacy table from object-storage code or vice
versa.

### ingestion_run

One row per complete object-storage-backed extraction/load attempt, keyed
by `run_id` (a UUID -- the same identifier minted for the run's own
object-storage key prefix, see `object_storage/keys.new_run_id`, never
re-derived). Records `vendor`, `endpoint`, `load_date`, `storage_bucket`/
`storage_run_prefix`, `requested_cursor`/`candidate_cursor`, and explicit
lifecycle `status`. **State machine:** `running` -> `published` ->
`loading` -> `committed`, or -> `failed` from any of the first three.
Each transition is its own guarded `UPDATE ... WHERE status = '<expected
prior status>'`; a zero-row update (the run was not in the expected
prior status) raises `errors.OperationalStateError` rather than being
treated as success -- this is what makes a retried `run_id` idempotent
(`create_ingestion_run`'s `ON CONFLICT (run_id) DO UPDATE`) without ever
letting an already-`committed` run be silently reset or reprocessed (see
"Run lifecycle and transaction boundaries" below). `started_at`/
`published_at`/`load_started_at`/`committed_at`/`failed_at`/`finished_at`
are each set only by the transition that reaches that status.
`extracted_count`/`accepted_count`/`rejected_count`/`inserted_count`/
`duplicate_count`/`page_count` are all `CHECK`-constrained non-negative
when populated. `failure_category`/`failure_message` are sanitized (see
`logging_utils.sanitize_error`, and `errors.ConnectorError.category` for
every exception category that can land here). Indexed by
`(vendor, endpoint, load_date DESC)` for endpoint history and by
`(status, started_at DESC)` for status-monitoring/investigation queries
(see "Operator queries" in `docs/RUNBOOK.md`).

### ingestion_page

One row per page of an ingestion_run, `UNIQUE (run_id, page_number)` (one
page_number per run) and separately `UNIQUE (object_key)` (every
published page object, across every run, is globally unique -- the
immutable-object-key rule). `page_number` is `CHECK`-constrained to
`BETWEEN 1 AND 999999`; `compressed_size_bytes`/`source_record_count`/
`accepted_count`/`rejected_count` are non-negative when populated;
`checksum` is `CHECK`-constrained to a 64-character lowercase hex string
(this repository's SHA-256 convention, added by migration 008).
`status` is a documented, `CHECK`-constrained vocabulary (`pending`,
`verified`, `loaded`, `failed`). An idempotent retry
(`state.insert_ingestion_page`'s `ON CONFLICT (run_id, page_number) DO
UPDATE ... WHERE`) may only update the MUTABLE verification/load-result
columns (`accepted_count`, `rejected_count`, `verified_at`, `status`) --
the `WHERE` clause on the conflict target requires the existing row's
`object_key`/`checksum`/`source_record_count` to already match what the
retry is asserting; if they disagree, the conflicting row is excluded
from the update (`RETURNING` yields nothing for it) and
`errors.OperationalStateError` is raised -- conflicting metadata for an
existing run/page fails loudly, it is never silently resolved either
way. Indexed by `run_id` (page-level detail for one known run) and by
`(status, run_id)` (added by migration 008, for "every page currently in
a given status across every run" investigation queries).

### ingestion_cursor

The sole authoritative cursor source for the object-storage-backed
workflow (never `source_watermarks`, which remains the legacy paginated
workflow's own watermark table -- the two are never allowed to both back
the same running workflow). Primary key `(vendor, endpoint)`. Records
`committed_cursor`, `successful_run_id` (a foreign key to
`ingestion_run`), `committed_at`, and `lock_version` (optimistic-
concurrency metadata, `bigint NOT NULL DEFAULT 0`). See "Cursor
concurrency and backward-movement protection" below for the full
locking/validation contract.

### rejected_record

One row per rejected source record, `UNIQUE (run_id, page_number,
record_position)` so a retried load never duplicates a rejection.
References `ingestion_run`. Records a stable `reason_code`
(`CHECK`-constrained, by migration 008, to
`endpoint_contract.RejectReason`'s exact five values: `not_an_object`,
`unsupported_endpoint`, `missing_source_id`, `missing_source_timestamp`,
`invalid_source_timestamp`) and a sanitized, bounded `detail`
(`CHECK`-constrained to <= 500 characters by migration 008 -- see
`endpoint_contract.Rejected`'s own docstring: never the raw field value
itself, only a description of the defect's *shape*). Records
`source_record_id`/`payload_hash` when safely available, and a durable
`raw_object_key` pointer back to the immutable page object in object
storage -- **this table never stores a copy of the rejected record's own
raw payload**; replay/investigation of the actual content always goes
through that pointer into immutable object storage, never through this
table. See "Rejected-record security and access" below for the full
access-control contract.

### schema_observation

Deterministic, PHI-free schema-drift observation: one row per distinct
`(vendor, endpoint, field_path, observed_type)` combination ever seen --
never one row per run/page. Stores the `field_path`/`observed_type`
themselves (never a field *value*) plus a deterministic SHA-256
`fingerprint` computed over sorted, canonicalized path/type pairs (see
`schema_observation.fingerprint`), so the same logical shape always
produces the same fingerprint regardless of record/observation order.
Tracks `first_observed_run_id`/`first_observed_page_number`/
`first_observed_at` (set once, on first insert, never overwritten) and
`last_observed_run_id`/`last_observed_page_number`/`last_observed_at`
(updated on every observing call). `occurrence_count` counts distinct
**occurrences** (distinct `(run_id, page_number)` pairs that have ever
observed this combination), never distinct *calls*: the upsert's
`occurrence_count` increment is conditional on this call's
`(run_id, page_number)` differing from what is already recorded as the
row's last-observed occurrence (`state.upsert_schema_observations`'s
`CASE ... IS DISTINCT FROM ...` -- the row's own already-persisted
`last_observed_run_id`/`last_observed_page_number` serve as the durable
occurrence identity; no additional column is needed). Replaying the same
run/page (a retried `load --run-id X`, or a run reprocessed after a
rollback) therefore never inflates `occurrence_count` -- see
`tests/integration/test_object_storage_pipeline_integration.py` for the
real-database proof. Indexed by `(vendor, endpoint, fingerprint)` and by
`(vendor, endpoint, last_observed_at DESC)` for drift-review queries.

## Run lifecycle and transaction boundaries

```
running --publish--> published --load-start--> loading --commit--> committed
   |                     |                         |
   +---------------------+----- fail (any) --------+---------------> failed
```

`running` and `published` are set (auto-committing, each its own small
transaction) entirely BEFORE any PostgreSQL load transaction opens: a
run is marked `published` only once every page object, the run
manifest, and the success marker are durable in object storage (see
`object_storage/publish.py` -- write-then-verify, immutable-once-written).
`loading` is set (also auto-committing) immediately before the ONE
atomic load transaction begins. Everything from that point --
`object_storage.verify.load_and_verify_manifest` re-verifying every page,
the per-page `COPY`-to-temp-then-merge into the raw table, rejected-
record inserts, schema-observation upserts, the `ingestion_page` upsert,
the cursor lock/validate/advance, and the final `committed` status write
-- happens inside that single transaction (`object_raw_loader.load_verified_run`,
called by `cli._run_object_load`), committed exactly once by the caller
after `load_verified_run` returns successfully, or rolled back entirely
by the caller on any exception, followed by a SEPARATE, freshly-committed
`state.mark_run_failed` write (never called from inside the transaction
it is reporting the failure of). No function that participates in that
atomic transaction (`mark_run_committed`, `insert_ingestion_page`,
`insert_rejected_records`, `upsert_schema_observations`,
`lock_cursor_for_update`, `commit_cursor`) ever calls `conn.commit()` or
`conn.rollback()` itself -- transaction-boundary ownership belongs
entirely to `cli._run_object_load`.

A zero-row lifecycle-transition update is never treated as success:
`mark_run_published`/`mark_run_load_started` roll back their own
single-statement transaction and raise `errors.OperationalStateError`;
`mark_run_committed` raises the same error without committing or rolling
back (the caller's rollback covers it). This is also what keeps a
`committed` run from ever being reset: a second `load --run-id X` for an
already-committed run fails at `mark_run_load_started` (the run is not
currently `published`), before any raw data, page, or cursor state is
touched. `mark_run_failed` is the one exception -- a zero-row update
there is a documented, intentional no-op (calling it for an
already-terminal run, e.g. one that raced to `committed` in another
process, must never overwrite that success).

## Cursor concurrency and backward-movement protection

`state.lock_cursor_for_update` creates the `(vendor, endpoint)` cursor
row at `(NULL, 0)` if it does not exist yet, then `SELECT ... FOR UPDATE`
it -- every load for a given `(vendor, endpoint)` serializes on this one
row regardless of whether it is that endpoint's first-ever load. A
concurrent second run for the same `(vendor, endpoint)` blocks on this
`SELECT` until the first run's transaction commits or rolls back, so two
concurrent runs can never silently overwrite one another's cursor
advance; runs for different `(vendor, endpoint)` pairs proceed fully
independently (no shared lock). The caller then compares the locked,
currently-committed cursor against its own `candidate_cursor`: a
candidate that would move the cursor backward raises `errors.CursorError`
immediately, before any commit. `state.commit_cursor` advances the
cursor via `UPDATE ... WHERE vendor = ... AND endpoint = ... AND
lock_version = <the version this transaction locked>`, incrementing
`lock_version`; because the row is held `FOR UPDATE` for the whole
transaction, a `lock_version` mismatch there can only mean a caller bug,
never a legitimate race -- it still raises `errors.CursorError` rather
than assuming success. A rolled-back transaction leaves the prior
cursor completely unchanged (ordinary PostgreSQL transaction semantics --
nothing in this cursor path ever writes outside the one transaction).

## Rejected-record security and access

`rejected_record` is treated as PHI-adjacent and access-restricted, more
so than the raw schema: `PUBLIC` has no access at all (explicit
`REVOKE`, defense in depth). `TRANSFORM_ROLE` (dbt) is never granted any
access, and `models/sources.yml` has no entry for it -- **dbt cannot
read `rejected_record`, in any configuration this repository ships**.
`INGEST_ROLE` (this connector) is granted **`INSERT` only** (migration
008 revokes the broader `SELECT`/`INSERT`/`UPDATE` migration 007
originally granted, matching `quarantined_records`' already-established
least-privilege pattern) -- `state.insert_rejected_records` only ever
`INSERT`s; reconciliation counts come from the `INSERT`'s own
affected-row count within the same transaction, never a `SELECT` against
this table. **No operational "rejected-record reviewer" role exists in
this repository's role model** -- an operator must explicitly
`CREATE ROLE`, then `GRANT SELECT ON <OPS_SCHEMA>.rejected_record TO
<that role>`, scoped to a specific person/process, before anyone can
read this table; this repository deliberately does not grant broad read
access by default. Replay of a rejected record's actual content always
goes through `raw_object_key` into the immutable, durable object-storage
page it came from -- this table itself never stores a copy of the raw
payload.


## Readiness

Extraction **may proceed** for the generic, already-implemented mechanisms themselves -- both the paginated JSON envelope/watermark/reconciliation contract (Sections 1-4, 6, 14; `secrets.py`, `pagination.py`, `paginated_loader.py`, `state.get_watermark`/`commit_watermark`) and the legacy manifest/snapshot mechanism (`api_client.py`, `manifest.py`, `extract.py`, `raw_loader.py`), plus the retry/timeout policy including OAuth's single-shot 401 recovery (Section 2, 5), the structural validation/quarantine safeguard (Section 15), the composite dedup-key strategy (Section 11), and the conservative PHI handling (Section 13), are already built and tested (`tests/unit/`, `tests/integration/`).

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

2026-08-27, against the state of this repository after completing the canonical operational-table contract for the object-storage-backed workflow: fixing the duplicate migration version 006 (renumbering `006_object_storage_raw_contract.sql` to 007), adding `migrations/008_operational_table_hardening.sql` (checksum-format validation, an enumerated/bounded `rejected_record.reason_code`/`detail`, least-privilege `INGEST_ROLE` grants on `rejected_record`, and a cross-run page-status index), correcting `state.upsert_schema_observations` so a replayed run/page no longer inflates `occurrence_count`, adding rowcount verification to every `ingestion_run` lifecycle transition and a fail-loudly guard on conflicting `ingestion_page` metadata (both via the new `errors.OperationalStateError`), and adding the seven exception classes (`CursorError`, `RawContractError`, `ObjectKeyError`, `ObjectStorageError`, `ImmutableObjectError`, `ObjectVerificationError`, `RunNotPublishedError`) and nine `IngestConfig` env-var aliases (`STAGING_SCHEMA`, `ANALYTICS_CORE_SCHEMA`, `ANALYTICS_MARTS_SCHEMA`, and the six `OBJECT_STORAGE_*` variables) that were referenced by `object_storage/`/`state.py`/`config.py` but missing from `errors.py`/`config.py`, which had left the entire object-storage-backed workflow unimportable outside a dependency-free sandbox -- see `migrations/008_operational_table_hardening.sql` and `src/tuva_ingest/{errors,config,state,cli}.py` for the exact changes. On top of the shared bounded-retry policy, OAuth client-credentials support, structural record quarantine, and hardened pagination safety limits (`src/tuva_ingest/{retry,oauth,validators,quarantine}.py`, `migrations/006_record_quarantine.sql`) on top of the paginated extraction/secret-manager/watermark mechanism (`src/tuva_ingest/{secrets,pagination,paginated_loader}.py`, `migrations/005_paginated_extraction_state.sql`). Re-verify this document (and update this date) whenever `src/tuva_ingest/{config,api_client,manifest,extract,raw_loader,state,secrets,pagination,paginated_loader,retry,oauth,validators,quarantine,errors,object_raw_loader,schema_observation}.py`, `docs/API_MANIFEST.md`, or the connected upstream source changes.
