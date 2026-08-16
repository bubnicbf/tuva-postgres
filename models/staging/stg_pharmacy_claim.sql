{{
  config(
    materialized='view'
  )
}}

-- Normalizes raw.pharmacy_claim (see models/sources.yml) into typed,
-- trimmed columns. See models/staging/stg_eligibility.sql's header for
-- the normalization approach (raw_field()/safe_date()/safe_numeric()/
-- safe_integer() -- macros/raw_field.sql, macros/safe_cast.sql).
--
-- Column names here match the Tuva Input Layer `pharmacy_claim`
-- contract directly (person_id not patient_id -- see models/final/
-- pharmacy_claim.sql's header for how that contract was confirmed) so
-- models/final/pharmacy_claim.sql's mapping is a plain select/cast, not
-- a rename. `quantity` is cast as an integer (safe_integer), matching
-- the contract's documented "positive integer" type for pharmacy claim
-- quantity -- distinct from medical_claim's numeric
-- service_unit_quantity.

with source as (

    select *
    from {{ source('raw', 'pharmacy_claim') }}

),

renamed as (

    select
        _snapshot_id,
        _source_row_number,
        _loaded_at,

        {{ raw_field('raw_row', 'claim_id') }}                            as claim_id,
        {{ safe_integer(raw_field('raw_row', 'claim_line_number')) }}     as claim_line_number,

        {{ raw_field('raw_row', 'person_id') }}                           as person_id,
        {{ raw_field('raw_row', 'member_id') }}                           as member_id,
        {{ raw_field('raw_row', 'payer') }}                               as payer,
        {{ raw_field('raw_row', 'plan') }}                                as plan,

        {{ raw_field('raw_row', 'prescribing_provider_id') }}             as prescribing_provider_npi,
        {{ raw_field('raw_row', 'dispensing_provider_id') }}              as dispensing_provider_npi,

        {{ safe_date(raw_field('raw_row', 'dispensing_date')) }}          as dispensing_date,

        {{ raw_field('raw_row', 'ndc_code') }}                            as ndc_code,
        {{ safe_integer(raw_field('raw_row', 'quantity')) }}              as quantity,
        {{ safe_integer(raw_field('raw_row', 'days_supply')) }}           as days_supply,
        {{ safe_integer(raw_field('raw_row', 'refills')) }}               as refills,

        {{ safe_date(raw_field('raw_row', 'paid_date')) }}                as paid_date,
        {{ safe_numeric(raw_field('raw_row', 'paid_amount')) }}           as paid_amount,
        {{ safe_numeric(raw_field('raw_row', 'allowed_amount')) }}        as allowed_amount,
        {{ safe_numeric(raw_field('raw_row', 'charge_amount')) }}         as charge_amount,
        {{ safe_numeric(raw_field('raw_row', 'coinsurance_amount')) }}    as coinsurance_amount,
        {{ safe_numeric(raw_field('raw_row', 'copayment_amount')) }}      as copayment_amount,
        {{ safe_numeric(raw_field('raw_row', 'deductible_amount')) }}     as deductible_amount,

        coalesce({{ raw_field('raw_row', 'data_source') }}, 'tuva')       as data_source

    from source

)

select * from renamed
