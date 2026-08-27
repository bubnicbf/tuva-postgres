{{
  config(
    materialized='table',
    tags=['input_layer']
  )
}}

-- Tuva Input Layer contract model: `eligibility`. This is the model the
-- Tuva package (packages.yml, pinned to tuva-health/the_tuva_project
-- 0.18.0) ref()'s directly by name -- its name and column set are the
-- connector's contract with the package, not an internal convention.
--
-- Column set and types were confirmed against two independent, current
-- (0.18.0-era) official Tuva sources rather than inferred from this
-- repository's own prior SQL:
--   1. thetuvaproject.com/connectors/claims-mapping-guide (the "eligibility"
--      section) -- the maintained field-by-field mapping documentation.
--   2. tuva-health/connector_template's `eligibility` model reference
--      (35 columns, https://github.com/tuva-health/connector_template),
--      the official reference implementation for building an Input
--      Layer connector.
-- See README.md "Architecture" / "Known limitations" for the full
-- verification record, including the one gap neither source could
-- close without live network access to the pinned package itself
-- (dbt_packages/the_tuva_project/ after `dbt deps`).
--
-- Primary key (per the mapping guide): person_id, member_id,
-- enrollment_start_date, enrollment_end_date, data_source (enforced in
-- models/final/schema.yml via dbt_utils.unique_combination_of_columns).
--
-- This is a thin contract projection: EVERY business rule -- field
-- extraction/trimming/casting (models/staging/stg_eligibility.sql),
-- member-identity crosswalk resolution and eligibility-span
-- consolidation (models/intermediate/int_eligibility_resolved.sql,
-- models/intermediate/int_eligibility_spans.sql) -- already happened
-- upstream. This model performs no joins, no deduplication, and no
-- multi-row logic of its own; it only selects/types the Input Layer's
-- exact column set from models/intermediate/int_eligibility_spans.sql,
-- which itself already excludes unmatched-identity and invalid-range
-- rows (see that model's header). Fields this connector's source data
-- does not provide anywhere are explicitly typed NULL, never a guess
-- (see the inline comments below for exactly which fields those are
-- and why).

with intermediate as (

    select * from {{ ref('int_eligibility_spans') }}

),

final as (

    select

        -- Identifiers. person_id is populated from the source's own
        -- person_id (existing "tuva" source) or resolved through the
        -- member crosswalk (the vendor source -- see
        -- models/intermediate/int_member_crosswalk.sql). A row whose
        -- identity could not be resolved never reaches this model (see
        -- models/intermediate/int_eligibility_resolved.sql's
        -- quarantine handling), so person_id is never NULL here.
        person_id::text                                       as person_id,
        member_id::text                                       as member_id,
        subscriber_id::text                                   as subscriber_id,

        -- Demographics. gender/race are not present in this source.
        cast(null as text)                                    as gender,
        cast(null as text)                                    as race,

        birth_date::date                                      as birth_date,
        death_date::date                                      as death_date,
        (case when death_date is not null then 1 else 0 end)::integer
                                                               as death_flag,

        -- Enrollment span (this table is the eligibility-span format,
        -- not member-month -- see models/intermediate/
        -- int_eligibility_spans.sql for how overlapping/adjacent source
        -- spans were consolidated into this span).
        enrollment_start_date::date                           as enrollment_start_date,
        enrollment_end_date::date                             as enrollment_end_date,

        payer::text                                           as payer,
        payer_type::text                                      as payer_type,
        plan::text                                            as plan,

        -- Medicare/Medicaid risk-adjustment fields. Populated directly
        -- from source; left NULL (never defaulted) when source is NULL,
        -- per the mapping guide's own documented CMS HCC mart fallback
        -- behavior.
        original_reason_entitlement_code::text                as original_reason_entitlement_code,
        dual_status_code::text                                as dual_status_code,
        medicare_status_code::text                            as medicare_status_code,

        group_id::text                                        as group_id,
        group_name::text                                      as group_name,

        -- Name/contact fields are not present in this source.
        cast(null as text)                                    as name_suffix,
        cast(null as text)                                    as first_name,
        cast(null as text)                                    as middle_name,
        cast(null as text)                                    as last_name,
        cast(null as text)                                    as email,
        cast(null as text)                                    as ethnicity,
        cast(null as text)                                    as social_security_number,
        cast(null as text)                                    as subscriber_relation,
        cast(null as text)                                    as address,
        cast(null as text)                                    as city,
        -- `state` (2-letter abbreviation) is intentionally NULL, not
        -- fips_state_code: the source only provides a numeric FIPS state
        -- code (see stg_eligibility.sql), and this repository has no
        -- authoritative FIPS->state-abbreviation crosswalk to map it
        -- through without inventing values (README.md "Known
        -- limitations").
        cast(null as text)                                    as state,
        cast(null as text)                                    as zip_code,
        cast(null as text)                                    as phone,

        data_source::text                                     as data_source,

        -- Source-file lineage metadata is not tracked by this raw
        -- loader (see src/tuva_ingest/raw_loader.py) beyond the
        -- _loaded_at column already used for freshness checks in
        -- models/sources.yml.
        cast(null as text)                                    as file_name,
        cast(null as date)                                    as file_date,
        cast(null as timestamp)                               as ingest_datetime

    from intermediate

)

select * from final
