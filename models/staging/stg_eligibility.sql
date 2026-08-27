{{
  config(
    materialized='view'
  )
}}

-- Normalizes raw.eligibility (see models/sources.yml) into typed,
-- trimmed columns: every field is pulled from `_effective_payload` via
-- the raw_field() macro (empty string -> NULL, see macros/raw_field.sql),
-- and every date field is cast defensively via the safe_date() macro (a
-- value that doesn't match the expected shape becomes a typed NULL,
-- never a failed dbt build -- see macros/safe_cast.sql).
--
-- This model reads TWO independent source-field vocabularies out of the
-- same raw payload, coalescing between them column-by-column (never
-- picking a single vocabulary at the model level), because this raw
-- table carries both shapes side by side (see docs/
-- CLAIMS_MAPPING_DECISIONS.md, "Why this mapping targets field names
-- the current test fixtures don't use"):
--   1. The existing "tuva" test source's Tuva-shaped fields
--      (person_id, member_id, enrollment_start_date/end_date, ...) --
--      already resolved identity (a direct person_id), no crosswalk
--      needed.
--   2. The incoming vendor-shaped extract documented in docs/
--      CLAIMS_MAPPING.csv (member_key, span_start_dt/span_end_dt,
--      coverage_type, ...) -- identity resolves only through
--      models/intermediate/int_member_crosswalk.sql, and span
--      consolidation happens in models/intermediate/
--      int_eligibility_spans.sql. Neither crosswalk resolution nor span
--      consolidation belongs here -- those are multi-row/joined
--      business rules (models/intermediate/'s job); this model only
--      ever extracts, trims, and casts one row at a time.
--
-- member_crosswalk_key and coverage_type have NO Tuva Input Layer
-- destination column of their own (see models/final/eligibility.sql) --
-- coverage_type is retained purely as a span-consolidation partition
-- key (docs/CLAIMS_MAPPING_DECISIONS.md decision 7); member_crosswalk_key
-- is retained purely as the crosswalk join key for vendor-shaped rows
-- that have no direct person_id.

with source as (

    select
        *,
        -- Compatibility path (see migrations/006_object_storage_raw_contract.sql):
        -- the new object-storage-backed loader (object_raw_loader.py) populates
        -- _raw_payload; the legacy CSV loader (raw_loader.py) populates raw_row
        -- only and always leaves _raw_payload NULL. coalesce() here is the one,
        -- explicit place these two independently-populated payload columns are
        -- ever reconciled -- every staging model reads _effective_payload, never
        -- raw_row or _raw_payload directly, so the two columns can never silently
        -- drift against each other in more than one place.
        coalesce(_raw_payload, raw_row) as _effective_payload
    from {{ source('raw', 'eligibility') }}

),

renamed as (

    select
        _snapshot_id,
        _source_row_number,
        _loaded_at,

        -- Direct identity (Tuva-shaped source only; NULL for the
        -- vendor-shaped source, which supplies member_crosswalk_key
        -- instead -- resolved in models/intermediate/
        -- int_eligibility_resolved.sql).
        {{ raw_field('_effective_payload', 'person_id') }}                          as person_id,
        {{ raw_field('_effective_payload', 'member_id') }}                          as member_id,
        {{ raw_field('_effective_payload', 'subscriber_id') }}                      as subscriber_id,

        -- Vendor-shaped identity/partition fields (docs/CLAIMS_MAPPING.csv).
        {{ raw_field('_effective_payload', 'member_key') }}                         as member_crosswalk_key,
        {{ raw_field('_effective_payload', 'coverage_type') }}                      as coverage_type,

        {{ safe_date(raw_field('_effective_payload', 'birth_date')) }}              as birth_date,
        {{ safe_date(raw_field('_effective_payload', 'death_date')) }}              as death_date,

        -- Enrollment span: Tuva-shaped enrollment_start_date/
        -- enrollment_end_date coalesced with the vendor-shaped
        -- span_start_dt/span_end_dt -- exactly one of each pair is ever
        -- populated on a given row (the two source vocabularies never
        -- overlap), so coalesce() is a safe, row-local rename+cast, not
        -- a business decision between competing values.
        coalesce(
            {{ safe_date(raw_field('_effective_payload', 'enrollment_start_date')) }},
            {{ safe_date(raw_field('_effective_payload', 'span_start_dt')) }}
        )                                                                           as enrollment_start_date,
        coalesce(
            {{ safe_date(raw_field('_effective_payload', 'enrollment_end_date')) }},
            {{ safe_date(raw_field('_effective_payload', 'span_end_dt')) }}
        )                                                                           as enrollment_end_date,

        {{ raw_field('_effective_payload', 'payer') }}                              as payer,
        {{ raw_field('_effective_payload', 'payer_type') }}                         as payer_type,
        {{ raw_field('_effective_payload', 'plan') }}                               as plan,

        {{ raw_field('_effective_payload', 'original_reason_entitlement_code') }}   as original_reason_entitlement_code,
        {{ raw_field('_effective_payload', 'dual_status_code') }}                   as dual_status_code,
        {{ raw_field('_effective_payload', 'medicare_status_code') }}               as medicare_status_code,

        {{ raw_field('_effective_payload', 'group_id') }}                           as group_id,
        {{ raw_field('_effective_payload', 'group_name') }}                         as group_name,

        -- Present in source, but NOT part of the Input Layer contract:
        -- the contract's `state` field expects a 2-letter USPS
        -- abbreviation, and this repository has no authoritative
        -- FIPS-code-to-abbreviation crosswalk to map through without
        -- inventing values (see models/final/eligibility.sql). Retained
        -- here, untyped-cast, for lineage/debugging visibility only --
        -- intentionally not selected by models/final/eligibility.sql.
        {{ raw_field('_effective_payload', 'fips_state_code') }}                    as fips_state_code,

        coalesce({{ raw_field('_effective_payload', 'data_source') }}, 'tuva')      as data_source

    from source

),

hashed as (

    -- Deterministic, payload-derived content fingerprint -- used by
    -- models/intermediate/int_eligibility_resolved.sql to collapse
    -- byte-identical duplicate deliveries and to order window functions
    -- deterministically (never by an unstable/nondeterministic order;
    -- see that model's header for how this is used). Built only from
    -- this model's own normalized business columns -- never from
    -- _loaded_at/_source_row_number, which differ per delivery even for
    -- an otherwise-identical row and would defeat duplicate detection.
    select
        *,
        md5(
            concat_ws(
                '|',
                coalesce(person_id, ''),
                coalesce(member_id, ''),
                coalesce(subscriber_id, ''),
                coalesce(member_crosswalk_key, ''),
                coalesce(coverage_type, ''),
                coalesce(birth_date::text, ''),
                coalesce(death_date::text, ''),
                coalesce(enrollment_start_date::text, ''),
                coalesce(enrollment_end_date::text, ''),
                coalesce(payer, ''),
                coalesce(payer_type, ''),
                coalesce(plan, ''),
                coalesce(original_reason_entitlement_code, ''),
                coalesce(dual_status_code, ''),
                coalesce(medicare_status_code, ''),
                coalesce(group_id, ''),
                coalesce(group_name, ''),
                coalesce(data_source, '')
            )
        ) as _row_hash
    from renamed

)

select * from hashed
