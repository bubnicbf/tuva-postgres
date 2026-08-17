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
    from {{ source('raw', 'pharmacy_claim') }}

),

renamed as (

    select
        _snapshot_id,
        _source_row_number,
        _loaded_at,

        {{ raw_field('_effective_payload', 'claim_id') }}                            as claim_id,
        {{ safe_integer(raw_field('_effective_payload', 'claim_line_number')) }}     as claim_line_number,

        {{ raw_field('_effective_payload', 'person_id') }}                           as person_id,
        {{ raw_field('_effective_payload', 'member_id') }}                           as member_id,
        {{ raw_field('_effective_payload', 'payer') }}                               as payer,
        {{ raw_field('_effective_payload', 'plan') }}                                as plan,

        {{ raw_field('_effective_payload', 'prescribing_provider_id') }}             as prescribing_provider_npi,
        {{ raw_field('_effective_payload', 'dispensing_provider_id') }}              as dispensing_provider_npi,

        {{ safe_date(raw_field('_effective_payload', 'dispensing_date')) }}          as dispensing_date,

        {{ raw_field('_effective_payload', 'ndc_code') }}                            as ndc_code,
        {{ safe_integer(raw_field('_effective_payload', 'quantity')) }}              as quantity,
        {{ safe_integer(raw_field('_effective_payload', 'days_supply')) }}           as days_supply,
        {{ safe_integer(raw_field('_effective_payload', 'refills')) }}               as refills,

        {{ safe_date(raw_field('_effective_payload', 'paid_date')) }}                as paid_date,
        {{ safe_numeric(raw_field('_effective_payload', 'paid_amount')) }}           as paid_amount,
        {{ safe_numeric(raw_field('_effective_payload', 'allowed_amount')) }}        as allowed_amount,
        {{ safe_numeric(raw_field('_effective_payload', 'charge_amount')) }}         as charge_amount,
        {{ safe_numeric(raw_field('_effective_payload', 'coinsurance_amount')) }}    as coinsurance_amount,
        {{ safe_numeric(raw_field('_effective_payload', 'copayment_amount')) }}      as copayment_amount,
        {{ safe_numeric(raw_field('_effective_payload', 'deductible_amount')) }}     as deductible_amount,

        coalesce({{ raw_field('_effective_payload', 'data_source') }}, 'tuva')       as data_source

    from source

)

select * from renamed
