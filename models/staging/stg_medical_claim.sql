{{
  config(
    materialized='view'
  )
}}

-- Normalizes raw.medical_claim (see models/sources.yml) into typed,
-- trimmed columns. See models/staging/stg_eligibility.sql's header for
-- the normalization approach (raw_field()/safe_date()/safe_numeric()/
-- safe_integer() -- macros/raw_field.sql, macros/safe_cast.sql).
--
-- Column names here match the Tuva Input Layer `medical_claim` contract
-- directly (person_id not patient_id; drg_code/drg_code_type not
-- ms_drg_code/ms_drg_code_type; hcpcs_code kept distinct from the ICD
-- `procedure_code_N` fields -- see models/final/medical_claim.sql's
-- header for how that contract was confirmed) so models/final/
-- medical_claim.sql's mapping is a plain select/cast, not a rename.

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
    from {{ source('raw', 'medical_claim') }}

),

renamed as (

    select
        _snapshot_id,
        _source_row_number,
        _loaded_at,

        {{ raw_field('_effective_payload', 'claim_id') }}                            as claim_id,
        {{ safe_integer(raw_field('_effective_payload', 'claim_line_number')) }}     as claim_line_number,
        {{ raw_field('_effective_payload', 'claim_type') }}                          as claim_type,

        {{ raw_field('_effective_payload', 'person_id') }}                           as person_id,
        {{ raw_field('_effective_payload', 'member_id') }}                           as member_id,
        {{ raw_field('_effective_payload', 'payer') }}                               as payer,
        {{ raw_field('_effective_payload', 'plan') }}                                as plan,

        {{ safe_date(raw_field('_effective_payload', 'claim_start_date')) }}         as claim_start_date,
        {{ safe_date(raw_field('_effective_payload', 'claim_end_date')) }}           as claim_end_date,
        {{ safe_date(raw_field('_effective_payload', 'claim_line_start_date')) }}    as claim_line_start_date,
        {{ safe_date(raw_field('_effective_payload', 'claim_line_end_date')) }}      as claim_line_end_date,
        {{ safe_date(raw_field('_effective_payload', 'admission_date')) }}           as admission_date,
        {{ safe_date(raw_field('_effective_payload', 'discharge_date')) }}           as discharge_date,
        {{ safe_date(raw_field('_effective_payload', 'paid_date')) }}                as paid_date,

        {{ raw_field('_effective_payload', 'admit_source_code') }}                   as admit_source_code,
        {{ raw_field('_effective_payload', 'admit_type_code') }}                     as admit_type_code,
        {{ raw_field('_effective_payload', 'discharge_disposition_code') }}          as discharge_disposition_code,
        {{ raw_field('_effective_payload', 'place_of_service_code') }}               as place_of_service_code,
        {{ raw_field('_effective_payload', 'bill_type_code') }}                      as bill_type_code,
        {{ raw_field('_effective_payload', 'revenue_center_code') }}                 as revenue_center_code,

        {{ raw_field('_effective_payload', 'drg_code_type') }}                       as drg_code_type,
        {{ raw_field('_effective_payload', 'drg_code') }}                            as drg_code,

        {{ safe_numeric(raw_field('_effective_payload', 'service_unit_quantity')) }} as service_unit_quantity,

        {{ raw_field('_effective_payload', 'hcpcs_code') }}                          as hcpcs_code,
        {{ raw_field('_effective_payload', 'hcpcs_modifier_1') }}                    as hcpcs_modifier_1,
        {{ raw_field('_effective_payload', 'hcpcs_modifier_2') }}                    as hcpcs_modifier_2,

        {{ raw_field('_effective_payload', 'rendering_id') }}                        as rendering_npi,
        {{ raw_field('_effective_payload', 'billing_id') }}                          as billing_npi,
        {{ raw_field('_effective_payload', 'facility_id') }}                         as facility_npi,

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
