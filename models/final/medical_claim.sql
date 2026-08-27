{{
  config(
    materialized='table',
    tags=['input_layer']
  )
}}

-- Tuva Input Layer contract model: `medical_claim`. See models/final/
-- eligibility.sql's header for the two-source contract-verification
-- approach (thetuvaproject.com/connectors/claims-mapping-guide plus
-- tuva-health/connector_template's reference implementation) and the
-- one documented gap (no live network access to diff against the
-- pinned package's own source -- see README.md "Known limitations").
--
-- 148 columns, matching connector_template's verified medical_claim
-- column count exactly: 5 identifiers, 2 payer/plan, 7 dates, 6
-- location/type codes, 2 DRG, 7 service-detail (HCPCS + modifiers), 5
-- provider identifiers, 7 financial, 51 diagnosis (type + 25 codes + 25
-- POA indicators), 51 procedure (type + 25 codes + 25 dates), 5
-- metadata.
--
-- Primary key: claim_id, claim_line_number, data_source (enforced in
-- models/final/schema.yml via dbt_utils.unique_combination_of_columns).
--
-- This is a thin contract projection: EVERY business rule -- field
-- extraction/casting (models/staging/stg_medical_claim.sql), member
-- identity resolution, claim-header date derivation, payer/plan
-- inheritance from eligibility, claim_type precedence, diagnosis/
-- procedure normalization, and claim lifecycle handling (models/
-- intermediate/int_medical_claim_lines.sql) -- already happened
-- upstream. This model performs no joins, no deduplication, and no
-- lifecycle logic of its own; it only selects/types the Input Layer's
-- exact column set, filtered to the rows that actually belong in the
-- Input Layer contract:
--   * `matched_member` -- a claim line whose member identity could not
--     be resolved through the crosswalk is retained with full lineage
--     in models/intermediate/int_medical_claim_lines.sql for
--     investigation (docs/CLAIMS_MAPPING_DECISIONS.md decision 5), but
--     is never loaded here, because the Input Layer contract requires
--     medical_claim.person_id NOT NULL and this repository never
--     invents an identity value. This is a documented divergence from
--     src/tuva_ingest/claims_mapping.py's own readiness-gate semantics,
--     which retains an unmatched line with person_id = NULL purely to
--     measure FK coverage -- a Python-only construct with no Input
--     Layer analog (see docs/CLAIMS_MAPPING_DECISIONS.md "Member
--     identity").
--   * `not is_superseded` -- an original claim (status 1) that has been
--     replaced by an adjustment (status 7, decision 2) is excluded here
--     so it is never double-counted alongside its replacement; it too
--     remains fully visible, flagged, in models/intermediate/
--     int_medical_claim_lines.sql. A void (status 8) is never excluded
--     -- its own (expected negative) amount stays in this table
--     alongside its original, so summing both nets to zero downstream,
--     per decision 2's documented sign-based cancellation behavior.
-- Fields this connector's source data does not provide anywhere are
-- explicitly typed NULL, never a guess.

with intermediate as (

    select * from {{ ref('int_medical_claim_lines') }}
    where matched_member
      and not is_superseded

),

final as (

    select

        -- Identifiers (5)
        claim_id::text                                        as claim_id,
        claim_line_number::integer                            as claim_line_number,
        claim_type::text                                      as claim_type,
        person_id::text                                       as person_id,
        member_id::text                                       as member_id,

        -- Payer / plan (2)
        payer::text                                           as payer,
        plan::text                                            as plan,

        -- Dates (7)
        claim_start_date::date                                as claim_start_date,
        claim_end_date::date                                  as claim_end_date,
        claim_line_start_date::date                            as claim_line_start_date,
        claim_line_end_date::date                              as claim_line_end_date,
        admission_date::date                                  as admission_date,
        discharge_date::date                                  as discharge_date,
        paid_date::date                                       as paid_date,

        -- Service location / claim-type classification codes (6)
        admit_source_code::text                               as admit_source_code,
        admit_type_code::text                                 as admit_type_code,
        discharge_disposition_code::text                      as discharge_disposition_code,
        place_of_service_code::text                            as place_of_service_code,
        bill_type_code::text                                  as bill_type_code,
        revenue_center_code::text                              as revenue_center_code,

        -- DRG (2)
        drg_code_type::text                                   as drg_code_type,
        drg_code::text                                        as drg_code,

        -- Service details: HCPCS/CPT + modifiers (7). Only modifiers 1-2
        -- are present in this source; 3-5 are typed NULL.
        service_unit_quantity::numeric                        as service_unit_quantity,
        hcpcs_code::text                                      as hcpcs_code,
        hcpcs_modifier_1::text                                 as hcpcs_modifier_1,
        hcpcs_modifier_2::text                                 as hcpcs_modifier_2,
        cast(null as text)                                    as hcpcs_modifier_3,
        cast(null as text)                                    as hcpcs_modifier_4,
        cast(null as text)                                    as hcpcs_modifier_5,

        -- Providers (5). This source distinguishes rendering/billing/
        -- facility NPIs directly; it does not provide TINs.
        rendering_npi::text                                   as rendering_npi,
        cast(null as text)                                    as rendering_tin,
        billing_npi::text                                     as billing_npi,
        cast(null as text)                                    as billing_tin,
        facility_npi::text                                    as facility_npi,

        -- Financial (7, numeric(38,2) per the mapping guide's documented
        -- convention). total_cost_amount is not separately supplied by
        -- this source (it is only distinct from allowed_amount when a
        -- pass-through per diem or similar applies).
        paid_amount::numeric(38, 2)                           as paid_amount,
        allowed_amount::numeric(38, 2)                        as allowed_amount,
        charge_amount::numeric(38, 2)                         as charge_amount,
        coinsurance_amount::numeric(38, 2)                    as coinsurance_amount,
        copayment_amount::numeric(38, 2)                      as copayment_amount,
        deductible_amount::numeric(38, 2)                     as deductible_amount,
        cast(null as numeric(38, 2))                          as total_cost_amount,

        -- Diagnosis (51). Populated for the vendor source (up to 3
        -- deduplicated, sequence-preserved codes -- models/intermediate/
        -- int_medical_claim_lines.sql); the existing "tuva" source
        -- supplies none, so positions 1-25 stay NULL for it, matching
        -- this table's prior, unchanged behavior for that source
        -- exactly.
        diagnosis_code_type::text                             as diagnosis_code_type,
        diagnosis_code_1::text                                as diagnosis_code_1,
        diagnosis_code_2::text                                as diagnosis_code_2,
        diagnosis_code_3::text                                as diagnosis_code_3,
{%- for i in range(4, 26) %}
        cast(null as text)                                    as diagnosis_code_{{ i }},
{%- endfor %}
{%- for i in range(1, 26) %}
        -- POA (present-on-admission) indicators are not supplied by
        -- either source.
        cast(null as text)                                    as diagnosis_poa_{{ i }},
{%- endfor %}

        -- Procedure (51). Populated for the vendor source (up to 2
        -- deduplicated codes + their dates); the existing "tuva" source
        -- supplies none. Note: when the vendor's proc_type is `HCPCS`
        -- (a professional-claim procedure/service code), it still lands
        -- in procedure_code_N here, not hcpcs_code above -- this
        -- follows docs/CLAIMS_MAPPING.csv's documented mapping exactly
        -- (proc_cd_N -> medical_claim.procedure_code_N regardless of
        -- proc_type), not an independent choice made in this model.
        procedure_code_type::text                             as procedure_code_type,
        procedure_code_1::text                                as procedure_code_1,
        procedure_code_2::text                                as procedure_code_2,
{%- for i in range(3, 26) %}
        cast(null as text)                                    as procedure_code_{{ i }},
{%- endfor %}
        procedure_date_1::date                                as procedure_date_1,
        procedure_date_2::date                                as procedure_date_2,
{%- for i in range(3, 26) %}
        cast(null as date)                                    as procedure_date_{{ i }},
{%- endfor %}

        -- Metadata (5)
        cast(null as integer)                                 as in_network_flag,
        data_source::text                                     as data_source,
        cast(null as text)                                    as file_name,
        cast(null as date)                                    as file_date,
        cast(null as timestamp)                               as ingest_datetime

    from intermediate

)

select * from final
