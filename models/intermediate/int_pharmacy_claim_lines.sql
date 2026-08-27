{{
  config(
    materialized='view'
  )
}}

-- Pharmacy claim intermediate model. No vendor-shaped source vocabulary
-- is documented for pharmacy claims yet (see stg_pharmacy_claim.sql's
-- header), so this model's only real multi-row responsibility today is
-- exact-duplicate-delivery collapse and genuine grain-conflict
-- surfacing -- the same rule models/intermediate/
-- int_medical_claim_lines.sql applies, kept here as its own model
-- (rather than folded into medical claim handling) so this domain's DAG
-- shape matches eligibility/medical_claim's raw -> staging ->
-- intermediate -> final lineage exactly, and so it is ready to take on
-- crosswalk resolution/lifecycle handling the moment a vendor pharmacy
-- mapping is documented (see docs/RUNBOOK.md "Known limitations").

with staging as (

    select * from {{ ref('stg_pharmacy_claim') }}

),

deduped as (

    select
        *,
        row_number() over (
            partition by claim_id, claim_line_number, data_source, _row_hash
            order by _loaded_at, _source_row_number
        ) as _duplicate_rank
    from staging

)

select
    claim_id,
    claim_line_number,
    person_id,
    member_id,
    payer,
    plan,
    prescribing_provider_npi,
    dispensing_provider_npi,
    dispensing_date,
    ndc_code,
    quantity,
    days_supply,
    refills,
    paid_date,
    paid_amount,
    allowed_amount,
    charge_amount,
    coinsurance_amount,
    copayment_amount,
    deductible_amount,
    data_source,
    _snapshot_id,
    _source_row_number,
    _loaded_at,
    _row_hash
from deduped
where _duplicate_rank = 1
