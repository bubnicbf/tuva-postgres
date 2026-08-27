{{
  config(
    materialized='view'
  )
}}

-- Normalizes raw.medical_claim (see models/sources.yml) into typed,
-- trimmed columns. See models/staging/stg_eligibility.sql's header for
-- the two-source-vocabulary approach this model shares with it
-- (raw_field()/safe_date()/safe_numeric()/safe_integer()/
-- cents_to_amount() -- macros/raw_field.sql, macros/safe_cast.sql):
--   1. The existing "tuva" test source (claim_id, person_id,
--      paid_amount in whole dollars, an already-classified claim_type,
--      ...).
--   2. The incoming vendor-shaped extract documented in docs/
--      CLAIMS_MAPPING.csv (clm_id, line_no, member_key, clm_status_code,
--      orig_clm_id, claim_form_code, paid_cents/allowed_cents/
--      charge_cents in integer cents, diag_cd_1..3/proc_cd_1..2, ...).
--
-- What this model deliberately does NOT do (see models/intermediate/
-- int_medical_claim_lines.sql, which owns all of it instead):
--   * derive claim_type from claim_form_code/bill_type_code/
--     place_of_service_code precedence (multi-signal business rule --
--     see macros/claim_type.sql) -- `source_claim_type_hint` below
--     carries through only the existing "tuva" source's ALREADY
--     explicit claim_type value, unchanged from source, as one input
--     to that rule. This column intentionally does NOT mirror the
--     Input Layer's own `claim_type` column name, to make clear it is
--     raw source fidelity, not the final derived value (see this
--     model's own historical anti-pattern this replaces, documented in
--     docs/CLAIMS_MAPPING_DECISIONS.md).
--   * resolve member_crosswalk_key -> person_id (a join).
--   * derive payer/plan for the vendor source (which has no payer/plan
--     field on the claims file at all -- see docs/CLAIMS_MAPPING.csv --
--     only on the eligibility file); resolved via a join to eligibility
--     in the intermediate layer.
--   * classify/resolve claim lifecycle (original/adjustment/void),
--     dedup exact-duplicate deliveries, or normalize the repeated
--     diag_cd_N/proc_cd_N columns into sequenced, deduplicated
--     positions -- all multi-row or cross-field business rules.

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

        -- Identifiers: Tuva-shaped claim_id/claim_line_number coalesced
        -- with the vendor-shaped clm_id/line_no -- exactly one
        -- vocabulary is ever populated per row.
        coalesce(
            {{ raw_field('_effective_payload', 'claim_id') }},
            {{ raw_field('_effective_payload', 'clm_id') }}
        )                                                                            as claim_id,
        coalesce(
            {{ safe_integer(raw_field('_effective_payload', 'claim_line_number')) }},
            {{ safe_integer(raw_field('_effective_payload', 'line_no')) }}
        )                                                                            as claim_line_number,

        -- Explicit source-supplied claim-type signal (existing "tuva"
        -- source only -- see macros/claim_type.sql for how this feeds
        -- the derived claim_type in the intermediate layer).
        {{ raw_field('_effective_payload', 'claim_type') }}                          as source_claim_type_hint,

        -- Lifecycle / adjustment-linking fields (vendor source only;
        -- docs/CLAIMS_MAPPING_DECISIONS.md decision 2). NULL (never
        -- defaulted) for the existing "tuva" source, which has no
        -- lifecycle concept -- every row from that source is treated as
        -- an unclassified/standalone line by the intermediate layer's
        -- lifecycle logic (never misclassified as a void or adjustment).
        {{ raw_field('_effective_payload', 'clm_status_code') }}                     as clm_status_code,
        {{ raw_field('_effective_payload', 'orig_clm_id') }}                         as orig_clm_id,
        {{ raw_field('_effective_payload', 'claim_form_code') }}                     as claim_form_code,

        {{ raw_field('_effective_payload', 'person_id') }}                           as person_id,
        {{ raw_field('_effective_payload', 'member_id') }}                           as member_id,
        {{ raw_field('_effective_payload', 'member_key') }}                          as member_crosswalk_key,
        {{ raw_field('_effective_payload', 'payer') }}                               as payer,
        {{ raw_field('_effective_payload', 'plan') }}                                as plan,

        -- Dates: the vendor source supplies one service date pair per
        -- line (service_from_dt/service_to_dt) and no separate
        -- claim-header-level dates; claim_start_date/claim_end_date for
        -- that source are derived from the claim's own lines by
        -- aggregation in models/intermediate/int_medical_claim_lines.sql
        -- (a cross-row rule), so they stay NULL here rather than being
        -- guessed at the line level.
        {{ safe_date(raw_field('_effective_payload', 'claim_start_date')) }}         as claim_start_date,
        {{ safe_date(raw_field('_effective_payload', 'claim_end_date')) }}           as claim_end_date,
        coalesce(
            {{ safe_date(raw_field('_effective_payload', 'claim_line_start_date')) }},
            {{ safe_date(raw_field('_effective_payload', 'service_from_dt')) }}
        )                                                                            as claim_line_start_date,
        coalesce(
            {{ safe_date(raw_field('_effective_payload', 'claim_line_end_date')) }},
            {{ safe_date(raw_field('_effective_payload', 'service_to_dt')) }}
        )                                                                            as claim_line_end_date,
        {{ safe_date(raw_field('_effective_payload', 'admission_date')) }}           as admission_date,
        {{ safe_date(raw_field('_effective_payload', 'discharge_date')) }}           as discharge_date,
        {{ safe_date(raw_field('_effective_payload', 'paid_date')) }}                as paid_date,

        {{ raw_field('_effective_payload', 'admit_source_code') }}                   as admit_source_code,
        {{ raw_field('_effective_payload', 'admit_type_code') }}                     as admit_type_code,
        {{ raw_field('_effective_payload', 'discharge_disposition_code') }}          as discharge_disposition_code,
        {{ raw_field('_effective_payload', 'place_of_service_code') }}               as place_of_service_code,
        {{ raw_field('_effective_payload', 'bill_type_code') }}                      as bill_type_code,
        coalesce(
            {{ raw_field('_effective_payload', 'revenue_center_code') }},
            {{ raw_field('_effective_payload', 'rev_code') }}
        )                                                                            as revenue_center_code,

        {{ raw_field('_effective_payload', 'drg_code_type') }}                       as drg_code_type,
        {{ raw_field('_effective_payload', 'drg_code') }}                            as drg_code,

        {{ safe_numeric(raw_field('_effective_payload', 'service_unit_quantity')) }} as service_unit_quantity,

        {{ raw_field('_effective_payload', 'hcpcs_code') }}                          as hcpcs_code,
        {{ raw_field('_effective_payload', 'hcpcs_modifier_1') }}                    as hcpcs_modifier_1,
        {{ raw_field('_effective_payload', 'hcpcs_modifier_2') }}                    as hcpcs_modifier_2,

        {{ raw_field('_effective_payload', 'rendering_id') }}                        as rendering_npi,
        {{ raw_field('_effective_payload', 'billing_id') }}                          as billing_npi,
        {{ raw_field('_effective_payload', 'facility_id') }}                         as facility_npi,

        -- Diagnosis / procedure repeated-column groups (vendor source
        -- only; docs/CLAIMS_MAPPING_DECISIONS.md decision 4). Sequence
        -- position, deduplication, and re-pivoting into the Input
        -- Layer's diagnosis_code_N/procedure_code_N columns all happen
        -- in models/intermediate/int_medical_claim_lines.sql -- this
        -- model only extracts/trims/casts each source column as-is.
        {{ raw_field('_effective_payload', 'diag_type') }}                           as diag_type,
        {{ raw_field('_effective_payload', 'diag_cd_1') }}                           as diag_cd_1,
        {{ raw_field('_effective_payload', 'diag_cd_2') }}                           as diag_cd_2,
        {{ raw_field('_effective_payload', 'diag_cd_3') }}                           as diag_cd_3,
        {{ raw_field('_effective_payload', 'proc_type') }}                           as proc_type,
        {{ raw_field('_effective_payload', 'proc_cd_1') }}                           as proc_cd_1,
        {{ safe_date(raw_field('_effective_payload', 'proc_dt_1')) }}                as proc_dt_1,
        {{ raw_field('_effective_payload', 'proc_cd_2') }}                           as proc_cd_2,
        {{ safe_date(raw_field('_effective_payload', 'proc_dt_2')) }}                as proc_dt_2,

        -- Financial: Tuva-shaped whole-dollar fields coalesced with the
        -- vendor-shaped integer-cents fields via cents_to_amount()
        -- (macros/safe_cast.sql -- Decimal division by 100, never
        -- float/integer truncation; docs/CLAIMS_MAPPING_DECISIONS.md
        -- decision 6). Exactly one vocabulary is ever populated per
        -- row, so coalesce() is a safe, row-local unit-conversion, not
        -- a business decision between competing values. Negative
        -- amounts (expected on a vendor void/reversal line) pass
        -- through unchanged -- this is unit conversion, not sign
        -- interpretation, which belongs to the intermediate layer's
        -- lifecycle handling.
        coalesce(
            {{ safe_numeric(raw_field('_effective_payload', 'paid_amount')) }},
            {{ cents_to_amount(raw_field('_effective_payload', 'paid_cents')) }}
        )                                                                            as paid_amount,
        coalesce(
            {{ safe_numeric(raw_field('_effective_payload', 'allowed_amount')) }},
            {{ cents_to_amount(raw_field('_effective_payload', 'allowed_cents')) }}
        )                                                                            as allowed_amount,
        coalesce(
            {{ safe_numeric(raw_field('_effective_payload', 'charge_amount')) }},
            {{ cents_to_amount(raw_field('_effective_payload', 'charge_cents')) }}
        )                                                                            as charge_amount,
        {{ safe_numeric(raw_field('_effective_payload', 'coinsurance_amount')) }}    as coinsurance_amount,
        {{ safe_numeric(raw_field('_effective_payload', 'copayment_amount')) }}      as copayment_amount,
        {{ safe_numeric(raw_field('_effective_payload', 'deductible_amount')) }}     as deductible_amount,

        -- data_source: an explicit source value always wins. Otherwise,
        -- a row that populates any vendor-only key (clm_id/member_key)
        -- is tagged 'incoming_source' (matching src/tuva_ingest/
        -- claims_mapping.py's own grain_key() default of the same
        -- name); everything else (the existing "tuva" fixture shape)
        -- keeps the prior 'tuva' default unchanged. This is a row-local
        -- default assignment, not a join or cross-row rule -- without
        -- it, every vendor-shaped row would silently collide with the
        -- "tuva" source's own data_source tag in the final composite
        -- primary key.
        coalesce(
            {{ raw_field('_effective_payload', 'data_source') }},
            case
                when {{ raw_field('_effective_payload', 'clm_id') }} is not null
                    or {{ raw_field('_effective_payload', 'member_key') }} is not null
                    then 'incoming_source'
                else 'tuva'
            end
        )                                                                            as data_source

    from source

),

hashed as (

    -- Deterministic, payload-derived content fingerprint (see
    -- stg_eligibility.sql's identical `hashed` CTE for why). Used by
    -- models/intermediate/int_medical_claim_lines.sql to collapse
    -- byte-identical duplicate deliveries and to detect genuine grain
    -- conflicts (same claim_id/claim_line_number/data_source, different
    -- content) -- never to pick an arbitrary "winner" between them.
    select
        *,
        md5(
            concat_ws(
                '|',
                coalesce(claim_id, ''), coalesce(claim_line_number::text, ''),
                coalesce(source_claim_type_hint, ''), coalesce(clm_status_code, ''),
                coalesce(orig_clm_id, ''), coalesce(claim_form_code, ''),
                coalesce(person_id, ''), coalesce(member_id, ''), coalesce(member_crosswalk_key, ''),
                coalesce(payer, ''), coalesce(plan, ''),
                coalesce(claim_start_date::text, ''), coalesce(claim_end_date::text, ''),
                coalesce(claim_line_start_date::text, ''), coalesce(claim_line_end_date::text, ''),
                coalesce(admission_date::text, ''), coalesce(discharge_date::text, ''), coalesce(paid_date::text, ''),
                coalesce(admit_source_code, ''), coalesce(admit_type_code, ''), coalesce(discharge_disposition_code, ''),
                coalesce(place_of_service_code, ''), coalesce(bill_type_code, ''), coalesce(revenue_center_code, ''),
                coalesce(drg_code_type, ''), coalesce(drg_code, ''), coalesce(service_unit_quantity::text, ''),
                coalesce(hcpcs_code, ''), coalesce(hcpcs_modifier_1, ''), coalesce(hcpcs_modifier_2, ''),
                coalesce(rendering_npi, ''), coalesce(billing_npi, ''), coalesce(facility_npi, ''),
                coalesce(diag_type, ''), coalesce(diag_cd_1, ''), coalesce(diag_cd_2, ''), coalesce(diag_cd_3, ''),
                coalesce(proc_type, ''), coalesce(proc_cd_1, ''), coalesce(proc_dt_1::text, ''),
                coalesce(proc_cd_2, ''), coalesce(proc_dt_2::text, ''),
                coalesce(paid_amount::text, ''), coalesce(allowed_amount::text, ''), coalesce(charge_amount::text, ''),
                coalesce(coinsurance_amount::text, ''), coalesce(copayment_amount::text, ''), coalesce(deductible_amount::text, ''),
                coalesce(data_source, '')
            )
        ) as _row_hash
    from renamed

)

select * from hashed
