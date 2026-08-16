{{
  config(
    materialized='table'
  )
}}

-- Tuva Input Layer contract model: `pharmacy_claim`. See models/final/
-- eligibility.sql's header for the contract-mapping approach and the
-- documented limitation on confirming the exact 0.18.0 column list
-- against the live Tuva source.
--
-- All source-fidelity normalization already happened in
-- models/staging/stg_pharmacy_claim.sql; this model selects/types the
-- Input Layer's expected column set. Fields our source does not
-- provide (e.g. NDC description, generic indicator) are typed NULLs
-- rather than invented mappings.

with staging as (

    select * from {{ ref('stg_pharmacy_claim') }}

),

final as (

    select
        claim_id::text                                        as claim_id,
        claim_line_number::integer                             as claim_line_number,

        patient_id::text                                       as patient_id,
        member_id::text                                        as member_id,
        payer::text                                             as payer,
        plan::text                                              as plan,

        prescribing_provider_npi::text                          as prescribing_provider_npi,
        dispensing_provider_npi::text                           as dispensing_provider_npi,

        dispensing_date::date                                   as dispensing_date,

        ndc_code::text                                          as ndc_code,
        cast(null as text)                                      as ndc_description,
        cast(null as text)                                      as generic_indicator,

        quantity::numeric                                       as quantity,
        days_supply::integer                                    as days_supply,
        refills::integer                                        as refills,

        paid_date::date                                         as paid_date,
        paid_amount::numeric                                    as paid_amount,
        allowed_amount::numeric                                 as allowed_amount,
        charge_amount::numeric                                  as charge_amount,
        coinsurance_amount::numeric                             as coinsurance_amount,
        copayment_amount::numeric                               as copayment_amount,
        deductible_amount::numeric                              as deductible_amount,

        data_source::text                                       as data_source

    from staging

)

select * from final
