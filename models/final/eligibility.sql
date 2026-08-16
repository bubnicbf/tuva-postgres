{{
  config(
    materialized='table'
  )
}}

-- Tuva Input Layer contract model: `eligibility`. This is the model the
-- Tuva package (packages.yml, pinned to tuva-health/the_tuva_project
-- 0.18.0) ref()'s directly -- its name and columns are the connector's
-- contract with the package, not an internal convention.
--
-- Column set follows the Tuva 0.18.0 claims Input Layer eligibility
-- table as documented at thetuvaproject.com. This repository's live
-- network access could not reach github.com/tuva-health to diff this
-- column list against the exact 0.18.0 source, so this mapping is an
-- explicitly documented assumption -- see README.md "Architecture" and
-- "Known limitations". Fields our source data does not provide are
-- surfaced as typed NULLs rather than guessed, per that same policy.
--
-- All source-fidelity normalization (trimming, empty-string handling,
-- date/numeric parsing) already happened in
-- models/staging/stg_eligibility.sql; no further business logic is
-- applied here beyond selecting/typing the contract's column set.

with staging as (

    select * from {{ ref('stg_eligibility') }}

),

final as (

    select
        patient_id::text                                  as patient_id,
        member_id::text                                    as member_id,
        subscriber_id::text                                 as subscriber_id,

        cast(null as text)                                  as gender,
        cast(null as text)                                  as race,

        birth_date::date                                    as birth_date,
        death_date::date                                    as death_date,
        (death_date is not null)                            as death_flag,

        enrollment_start_date::date                         as enrollment_start_date,
        enrollment_end_date::date                           as enrollment_end_date,

        payer::text                                         as payer,
        payer_type::text                                    as payer_type,
        plan::text                                          as plan,

        original_reason_entitlement_code::text              as original_reason_entitlement_code,
        dual_status_code::text                              as dual_status_code,
        medicare_status_code::text                          as medicare_status_code,

        cast(null as text)                                  as insurance_type_code,
        cast(null as text)                                  as point_of_service_plan_code,
        cast(null as text)                                  as part_c_indicator,
        cast(null as text)                                  as part_d_indicator,

        group_id::text                                      as group_id,
        group_name::text                                    as group_name,
        cast(null as text)                                  as irs_group_name,

        fips_state_code::text                                as fips_state_code,

        data_source::text                                   as data_source

    from staging

)

select * from final
