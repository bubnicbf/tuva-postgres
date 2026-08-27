{{
  config(
    materialized='table',
    tags=['input_layer']
  )
}}

-- Tuva Input Layer contract model: `pharmacy_claim`. See models/final/
-- eligibility.sql's header for the two-source contract-verification
-- approach and the one documented gap (README.md "Known limitations").
--
-- 25 columns, matching both thetuvaproject.com/connectors/
-- claims-mapping-guide's field list and tuva-health/connector_template's
-- verified pharmacy_claim reference implementation exactly.
--
-- Primary key: claim_id, claim_line_number, data_source (enforced in
-- models/final/schema.yml via dbt_utils.unique_combination_of_columns).
--
-- Financial fields use numeric(38,2), per the mapping guide's
-- documented convention for claims financial fields (this repository
-- targets PostgreSQL only, so there is no cross-database reason to use
-- a lower-precision float type here).
--
-- This is a thin contract projection: all row-local normalization
-- already happened in models/staging/stg_pharmacy_claim.sql, and
-- exact-duplicate-delivery collapse already happened in
-- models/intermediate/int_pharmacy_claim_lines.sql (see that model's
-- header for why this domain has no crosswalk/lifecycle logic yet).
-- This model performs no joins, no deduplication, and no business
-- logic of its own; it only selects/types the Input Layer's full column
-- set and fills every field our source does not provide
-- (in_network_flag, file lineage) with an explicitly typed NULL rather
-- than an invented mapping.

with intermediate as (

    select * from {{ ref('int_pharmacy_claim_lines') }}

),

final as (

    select

        claim_id::text                                        as claim_id,
        claim_line_number::integer                             as claim_line_number,

        person_id::text                                       as person_id,
        member_id::text                                       as member_id,
        payer::text                                           as payer,
        plan::text                                            as plan,

        prescribing_provider_npi::text                         as prescribing_provider_npi,
        dispensing_provider_npi::text                          as dispensing_provider_npi,

        dispensing_date::date                                 as dispensing_date,

        ndc_code::text                                        as ndc_code,

        quantity::integer                                     as quantity,
        days_supply::integer                                  as days_supply,
        refills::integer                                      as refills,

        paid_date::date                                       as paid_date,
        paid_amount::numeric(38, 2)                           as paid_amount,
        allowed_amount::numeric(38, 2)                        as allowed_amount,
        charge_amount::numeric(38, 2)                         as charge_amount,
        coinsurance_amount::numeric(38, 2)                    as coinsurance_amount,
        copayment_amount::numeric(38, 2)                       as copayment_amount,
        deductible_amount::numeric(38, 2)                     as deductible_amount,

        -- Not supplied by this source.
        cast(null as integer)                                 as in_network_flag,

        data_source::text                                     as data_source,

        cast(null as text)                                    as file_name,
        cast(null as date)                                    as file_date,
        cast(null as timestamp)                               as ingest_datetime

    from intermediate

)

select * from final
