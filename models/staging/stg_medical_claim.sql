{{
  config(
    materialized='view'
  )
}}

-- Normalizes raw.medical_claim (see models/sources.yml) into typed,
-- trimmed columns. See models/staging/stg_eligibility.sql's header for
-- the normalization approach (raw_field()/safe_date()/safe_numeric()/
-- safe_integer() -- macros/raw_field.sql, macros/safe_cast.sql).

with source as (

    select *
    from {{ source('raw', 'medical_claim') }}

),

renamed as (

    select
        _snapshot_id,
        _source_row_number,
        _loaded_at,

        {{ raw_field('raw_row', 'claim_id') }}                            as claim_id,
        {{ safe_integer(raw_field('raw_row', 'claim_line_number')) }}     as claim_line_number,

        {{ raw_field('raw_row', 'person_id') }}                           as patient_id,
        {{ raw_field('raw_row', 'member_id') }}                           as member_id,
        {{ raw_field('raw_row', 'payer') }}                               as payer,
        {{ raw_field('raw_row', 'plan') }}                                as plan,

        {{ raw_field('raw_row', 'claim_type') }}                         as claim_type,

        {{ safe_date(raw_field('raw_row', 'claim_start_date')) }}         as claim_start_date,
        {{ safe_date(raw_field('raw_row', 'claim_end_date')) }}           as claim_end_date,
        {{ safe_date(raw_field('raw_row', 'claim_line_start_date')) }}    as claim_line_start_date,
        {{ safe_date(raw_field('raw_row', 'claim_line_end_date')) }}      as claim_line_end_date,
        {{ safe_date(raw_field('raw_row', 'admission_date')) }}           as admission_date,
        {{ safe_date(raw_field('raw_row', 'discharge_date')) }}           as discharge_date,

        {{ raw_field('raw_row', 'admit_source_code') }}                   as admit_source_code,
        {{ raw_field('raw_row', 'admit_type_code') }}                     as admit_type_code,
        {{ raw_field('raw_row', 'discharge_disposition_code') }}          as discharge_disposition_code,

        {{ raw_field('raw_row', 'place_of_service_code') }}               as place_of_service_code,
        {{ raw_field('raw_row', 'bill_type_code') }}                      as bill_type_code,

        {{ raw_field('raw_row', 'drg_code_type') }}                       as ms_drg_code_type,
        {{ raw_field('raw_row', 'drg_code') }}                            as ms_drg_code,

        {{ raw_field('raw_row', 'revenue_center_code') }}                 as revenue_center_code,
        {{ safe_numeric(raw_field('raw_row', 'service_unit_quantity')) }} as quantity,

        {{ raw_field('raw_row', 'hcpcs_code') }}                          as procedure_code,
        'hcpcs'                                                          as procedure_code_type,
        {{ raw_field('raw_row', 'hcpcs_modifier_1') }}                    as procedure_modifier_1,
        {{ raw_field('raw_row', 'hcpcs_modifier_2') }}                    as procedure_modifier_2,

        {{ raw_field('raw_row', 'rendering_id') }}                        as rendering_npi,
        {{ raw_field('raw_row', 'billing_id') }}                          as billing_npi,
        {{ raw_field('raw_row', 'facility_id') }}                         as facility_npi,

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
