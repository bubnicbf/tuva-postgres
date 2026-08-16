# Source contract: the `tuva-ingest` extraction source

This document is the operational source contract for whatever upstream
HTTP source is configured as this connector's extraction target
(`TUVA_API_MANIFEST_URL` / `SOURCE_NAME`, see `src/tuva_ingest/config.py`
and `scripts/setup_env.example`). It exists so that no extraction code
change ships without the vendor-facing facts below being written down,
checked, and kept current -- see "Readiness" and "Automated validation".

It complements, and does not replace, `docs/API_MANIFEST.md` (the wire
format only) and `docs/RUNBOOK.md` (day-to-day operation). Implementation:
`src/tuva_ingest/{config,api_client,manifest,extract,raw_loader,state}.py`.
Validated by: `tests/unit/test_source_contract.py`.

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
- Secret management: **Verified.** `TUVA_API_TOKEN` is read only from the process environment (`config.py`); `.env` is git-ignored (`.gitignore`); `IngestConfig.safe_dict()`/`__repr__` redact it as `"***REDACTED***"`; `docs/RUNBOOK.md` "Security notes" documents rotation via `.env`/secret store with no code change required.
- Secrets and PHI must never be committed to this repository. Examples below are redacted placeholders only, consistent with `scripts/setup_env.example`'s empty-string convention -- no real token, DSN, or patient data appears in this document.

Redacted example (never a real value):

```
export TUVA_API_MANIFEST_URL="https://example.invalid/snapshots/latest/manifest.json"
export TUVA_API_TOKEN="<redacted>"
```

## 3. Endpoints and expected record grain

Two endpoint *shapes*, not fixed paths (**Verified**, `api_client.py` / `manifest.py`):

1. `GET {TUVA_API_MANIFEST_URL}` -- returns the manifest JSON document (see `docs/API_MANIFEST.md` "Shape", reproduced below). Purpose: enumerate one snapshot's per-table CSV artifacts and their checksums. Grain: one document per snapshot, not itself record-grained.
2. `GET {artifact.url}` (one per table, URL supplied inside the manifest, never a fixed path) -- returns one complete CSV file for that table. Purpose: full extract of one raw table's current complete contents.

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

- Verified mechanism: **none.** This connector does not paginate. Each artifact URL returns one complete CSV file per table per snapshot; `ApiClient.download_artifact` streams the entire response body. No page/cursor/offset parameter or `Link` header handling exists in `api_client.py` or `manifest.py`.
- Request parameters / response fields: not applicable.
- Termination condition: **Verified.** End of the streamed HTTP response body, with a hard safety ceiling: `DEFAULT_MAX_ARTIFACT_BYTES` (5 GiB) and `size_limit = min(max_artifact_bytes, max(declared_size * 2, 1024))` abort an oversized or runaway response before it exhausts memory or disk (`api_client.py`).
- Ordering guarantees: **Unverified.** CSV row order is whatever the source emits. `_source_row_number` (`raw_loader.py`) is assigned sequentially at load time as a within-file position marker only, not a vendor-guaranteed sort order.
- Token expiry / safe restart: not applicable to pagination; see Section 7 for this design's snapshot-level analogue.
- Duplicate/missing records across boundaries: not applicable -- there are no pages. A whole raw table is replaced by `TRUNCATE` + `COPY` per snapshot (`raw_loader.load_table`), so partial or duplicated rows cannot straddle a boundary by construction.
- **Decision, restated in Readiness:** if a real, eventually-connected vendor's actual API is paginated (e.g. a JSON REST API rather than bulk CSV), this section and the underlying client must be revised together before extraction against that vendor is considered ready.

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

**Verified:** this connector does not perform field-based incremental/delta extraction. Every `extract` run fetches the manifest's current full state and (unless byte-identical to the already-published snapshot for that `snapshot_id`) downloads complete, full-population CSV artifacts for all three raw tables (`extract.extract_snapshot`; `raw_loader.py`'s `TRUNCATE` + `COPY` per table per run).

- Cursor field, data type, precision, timezone, ordering, nullability, inclusive/exclusive filtering, tie-breaking: not applicable. **Decision, not a gap:** no `updated_at`/service-date filter parameter is ever sent to the source. "Incremental-ness" is expressed at the snapshot level (`snapshot_id`, `created_at`), never at the row level.
- Overlap/lookback window: not applicable at the row level. At the snapshot level, none is configured -- each run either idempotently skips (identical `snapshot_id` and content already published) or fully replaces (`extract.check_idempotent_or_conflicting`).
- Watermark persistence and restart: **Verified.** The watermark is `RawSnapshotStore.current_snapshot_id()` (a text file at `RAW_DATA_DIR/{source}/current`) plus `ingest_ops.ingestion_runs.snapshot_id`. Restart safety is snapshot-level idempotency: same `snapshot_id` + same content is a no-op; same `snapshot_id` + different content is a loud `ExtractError`, never a silent overwrite.
- Why this captures corrections and late-arriving changes: **Decision/assumption.** Full-snapshot replacement inherently captures any correction the vendor has already applied to its own current state as of `created_at`, at the cost of no row-level change history and no way to determine from the wire format alone which rows changed between two snapshots. This is an explicit trade-off; see Section 7.

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

- Vendor-provided totals: **Verified absent** from the current wire contract. The manifest (`manifest.py`) provides only `sha256` and `size_bytes` per artifact -- no vendor-supplied record count, claim count, line count, or billed/paid/denied amount total appears anywhere in `docs/API_MANIFEST.md`'s shape.
- Compensating controls actually implemented (**Verified**):
  - Per-artifact SHA-256 and byte-count verification at download time (`ApiClient.download_artifact`) and again at raw-load time (`raw_loader.verify_file_checksum`) -- detects corruption/tampering, not business-total correctness.
  - Per-table row counts recorded at load time: `ingest_ops.table_loads.row_count` (`migrations/002_ingestion_control.sql`; `state.mark_table_load_succeeded`), aggregated per run into `ingest_ops.ingestion_runs.rows_loaded` (a JSON object such as `{"eligibility": 1000, "medical_claim": 5000, "pharmacy_claim": 2000}`, `state.mark_succeeded`).
  - `state.table_load_row_counts()` lets an operator or test confirm every expected raw table was actually loaded for a given run, not just that the run's overall status is `succeeded`.
- Comparison tolerances, frequency, alert thresholds, investigation procedure: **Unverified.** None are defined in this repository; row counts are recorded but nothing currently compares them run-over-run or alerts on an unexpected swing.
- **Blocking note:** without vendor-provided totals, business-level reconciliation (for example, "did we receive every claim the payer sent this period") cannot be confirmed by this connector alone -- only structural/technical completeness (checksums plus row counts) is currently verifiable.

## Readiness

Extraction **may proceed** for the generic, already-implemented manifest/snapshot mechanism itself -- the wire protocol (Sections 1-4), the retry policy (Section 5), the composite dedup-key strategy (Section 11), and the conservative PHI handling (Section 13) are already built and tested (`api_client.py`, `manifest.py`, `extract.py`, `raw_loader.py`, plus `tests/unit/` and `tests/integration/`).

Extraction against a **real, specific external vendor is blocked** until all of the following are resolved for that vendor:

1. Its actual base URL, authentication flow (confirm static bearer token vs. OAuth/other), and published rate limits are confirmed (Sections 1, 2, 5).
2. Its actual CSV column layout is confirmed to supply the fields the Input Layer contract requires, and whether it carries claim-status/denial/reversal columns (Sections 3, 8).
3. PHI storage, encryption, access-control, and retention controls are put in place for `RAW_DATA_DIR` and the `raw`/`ingest_ops` schemas beyond database role grants -- currently unverified/absent (Section 13).
4. A deletion/tombstone or snapshot-diff strategy is decided if any downstream consumer needs delete-detection (Section 8).
5. Backfill volume is estimated and a batching/runtime plan is confirmed to fit operational constraints -- currently entirely unverified (Section 9).
6. Reconciliation tolerances and alerting are defined even in the absence of vendor-provided totals (Section 14).

Until items 1-6 are resolved for a specific vendor, this connector must only be pointed at a manifest-contract-compliant test or mock server (as already done in `tests/unit/` and `tests/integration/`), never at a live vendor endpoint carrying real PHI.

## Owner

This repository has no `CODEOWNERS` file or other documented team-ownership convention as of this writing (confirmed by repository search). An owner or responsible team should be assigned before this connector is pointed at a real vendor; until then, changes to this document and the extraction code it governs should be reviewed the same way any other change to `src/tuva_ingest/` is reviewed.

## Last verified

2026-08-16, against commit `5b3786a` (`feat(etl): enforce Tuva input layer contract`) -- the repository state this document was written against. Re-verify this document (and update this date) whenever `src/tuva_ingest/{config,api_client,manifest,extract,raw_loader,state}.py`, `docs/API_MANIFEST.md`, or the connected upstream source changes.
