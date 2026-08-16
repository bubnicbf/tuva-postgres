{{
  config(
    materialized='view'
  )
}}

-- Normalizes raw.eligibility (see models/sources.yml) into typed,
-- trimmed columns: every field is pulled from `raw_row` via the
-- raw_field() macro (empty string -> NULL, see macros/raw_field.sql),
-- and every date/numeric field is cast defensively via the safe_date()/
-- safe_numeric() macros (a value that doesn't match the expected shape
-- becomes a typed NULL, never a failed dbt build -- see
-- macros/safe_cast.sql). No Tuva-specific business logic here; that
-- begins in models/final/eligibility.sql and continues in the Tuva
-- package itself.

with source as (

    select *
    from {{ source('raw', 'eligibility') }}

),

renamed as (

    select
        _snapshot_id,
        _source_row_number,
        _loaded_at,

        {{ raw_field('raw_row', 'person_id') }}                          as patient_id,
        {{ raw_field('raw_row', 'member_id') }}                          as member_id,
        {{ raw_field('raw_row', 'subscriber_id') }}                      as subscriber_id,

        {{ safe_date(raw_field('raw_row', 'birth_date')) }}              as birth_date,
        {{ safe_date(raw_field('raw_row', 'death_date')) }}              as death_date,

        {{ safe_date(raw_field('raw_row', 'enrollment_start_date')) }}   as enrollment_start_date,
        {{ safe_date(raw_field('raw_row', 'enrollment_end_date')) }}     as enrollment_end_date,

        {{ raw_field('raw_row', 'payer') }}                              as payer,
        {{ raw_field('raw_row', 'payer_type') }}                         as payer_type,
        {{ raw_field('raw_row', 'plan') }}                               as plan,

        {{ raw_field('raw_row', 'original_reason_entitlement_code') }}   as original_reason_entitlement_code,
        {{ raw_field('raw_row', 'dual_status_code') }}                   as dual_status_code,
        {{ raw_field('raw_row', 'medicare_status_code') }}               as medicare_status_code,

        {{ raw_field('raw_row', 'group_id') }}                           as group_id,
        {{ raw_field('raw_row', 'group_name') }}                         as group_name,

        {{ raw_field('raw_row', 'fips_state_code') }}                    as fips_state_code,

        coalesce({{ raw_field('raw_row', 'data_source') }}, 'tuva')      as data_source

    from source

)

select * from renamed
