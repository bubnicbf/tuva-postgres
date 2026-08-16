{{
  config(
    materialized='table'
  )
}}

-- Tuva Input Layer contract model: `medical_claim`. See models/final/
-- eligibility.sql's header for the contract-mapping approach and the
-- documented limitation on confirming the exact 0.18.0 column list
-- against the live Tuva source.
--
-- All source-fidelity normalization already happened in
-- models/staging/stg_medical_claim.sql; this model selects/types the
-- Input Layer's expected column set and fills fields our source does
-- not provide (diagnosis codes, provider taxonomy, etc.) with typed
-- NULLs rather than invented mappings.

with staging as (

    select * from {{ ref('stg_medical_claim') }}

),

final as (

    select
        claim_id::text                                      as claim_id,
        claim_line_number::integer                           as claim_line_number,

        patient_id::text                                     as patient_id,
        member_id::text                                      as member_id,
        payer::text                                           as payer,
        plan::text                                            as plan,

        claim_type::text                                      as claim_type,

        claim_start_date::date                                as claim_start_date,
        claim_end_date::date                                  as claim_end_date,
        claim_line_start_date::date                           as claim_line_start_date,
        claim_line_end_date::date                             as claim_line_end_date,
        admission_date::date                                  as admission_date,
        discharge_date::date                                  as discharge_date,

        admit_source_code::text                               as admit_source_code,
        admit_type_code::text                                 as admit_type_code,
        discharge_disposition_code::text                      as discharge_disposition_code,

        place_of_service_code::text                           as place_of_service_code,
        bill_type_code::text                                  as bill_type_code,

        ms_drg_code_type::text                                as ms_drg_code_type,
        ms_drg_code::text                                     as ms_drg_code,

        revenue_center_code::text                             as revenue_center_code,
        quantity::numeric                                     as quantity,

        procedure_code::text                                  as procedure_code,
        procedure_code_type::text                             as procedure_code_type,
        procedure_modifier_1::text                            as procedure_modifier_1,
        procedure_modifier_2::text                            as procedure_modifier_2,

        cast(null as text)                                    as principal_diagnosis_code,
        cast(null as text)                                    as principal_diagnosis_code_type,

        rendering_npi::text                                   as rendering_npi,
        billing_npi::text                                     as billing_npi,
        facility_npi::text                                    as facility_npi,

        paid_date::date                                       as paid_date,
        paid_amount::numeric                                  as paid_amount,
        allowed_amount::numeric                               as allowed_amount,
        charge_amount::numeric                                as charge_amount,
        coinsurance_amount::numeric                           as coinsurance_amount,
        copayment_amount::numeric                             as copayment_amount,
        deductible_amount::numeric                            as deductible_amount,

        data_source::text                                     as data_source

    from staging

)

select * from final
