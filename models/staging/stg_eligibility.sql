{{
  config(
    materialized='view'
  )
}}

-- Normalizes raw.eligibility (see models/sources.yml) into typed,
-- trimmed columns: every field is pulled from `raw_row` via the
-- raw_field() macro (empty string -> NULL, see macros/raw_field.sql),
-- and every date field is cast defensively via the safe_date() macro (a
-- value that doesn't match the expected shape becomes a typed NULL,
-- never a failed dbt build -- see macros/safe_cast.sql). No
-- Tuva-specific business logic here; that begins in
-- models/final/eligibility.sql and continues in the Tuva package
-- itself.
--
-- Column names here match the Tuva Input Layer `eligibility` contract
-- directly (person_id, not patient_id -- see models/final/eligibility.sql's
-- header for how that contract was confirmed) so models/final/
-- eligibility.sql's mapping is a plain select/cast, not a rename.

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

        {{ raw_field('_effective_payload', 'person_id') }}                          as person_id,
        {{ raw_field('_effective_payload', 'member_id') }}                          as member_id,
        {{ raw_field('_effective_payload', 'subscriber_id') }}                      as subscriber_id,

        {{ safe_date(raw_field('_effective_payload', 'birth_date')) }}              as birth_date,
        {{ safe_date(raw_field('_effective_payload', 'death_date')) }}              as death_date,

        {{ safe_date(raw_field('_effective_payload', 'enrollment_start_date')) }}   as enrollment_start_date,
        {{ safe_date(raw_field('_effective_payload', 'enrollment_end_date')) }}     as enrollment_end_date,

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

)

select * from renamed
