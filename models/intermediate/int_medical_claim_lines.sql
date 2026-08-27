{{
  config(
    materialized='view'
  )
}}

-- The medical-claim intermediate model: owns every multi-row/joined
-- business rule this Input Layer connector applies to medical claim
-- lines (see README.md "Architecture" and docs/
-- CLAIMS_MAPPING_DECISIONS.md for the executable-spec references each
-- section below implements in SQL):
--   1. Exact-duplicate-delivery collapse (decision 1) and genuine
--      grain-conflict surfacing (never silently resolved -- see
--      tests/dbt/assert_no_medical_claim_grain_conflicts.sql).
--   2. Member identity resolution via
--      models/intermediate/int_member_crosswalk.sql (decision 5).
--   3. Claim-header date derivation (claim_start_date/claim_end_date)
--      by aggregating a claim's own lines when the source does not
--      supply header-level dates directly.
--   4. payer/plan derivation for the vendor source (which has no
--      payer/plan field on the claims file at all -- only on the
--      eligibility file, see docs/CLAIMS_MAPPING.csv) via a join to
--      models/intermediate/int_eligibility_spans.sql for the coverage
--      active on the claim's own service date.
--   5. Deterministic claim_type derivation (decision 3;
--      macros/claim_type.sql).
--   6. Diagnosis/procedure repeated-column normalization: sequence
--      position, deduplication, primary-diagnosis semantics
--      (decision 4).
--   7. Claim lifecycle handling: original (status 1) / adjustment
--      (status 7) / void (status 8), with an adjustment excluding its
--      superseded original from what reaches models/final/
--      medical_claim.sql (never double-counted), while a void's
--      (expected negative) amount stays alongside its original so the
--      two net to zero when summed downstream (decision 2). Every row
--      -- including a superseded original -- survives in THIS model
--      with an explicit `is_superseded`/`lifecycle_action` flag for
--      auditability; only models/final/medical_claim.sql's own
--      `where` filters anything out.

with staging as (

    select * from {{ ref('stg_medical_claim') }}

),

deduped as (

    -- Exact-duplicate-delivery collapse (decision 1): a byte-identical
    -- repeat row (same claim_id/claim_line_number/data_source AND every
    -- other normalized field -- i.e. the same _row_hash) collapses to
    -- the earliest-delivered copy, ordered deterministically by stable
    -- source metadata. Two rows that share (claim_id, claim_line_number,
    -- data_source) but differ in content produce DIFFERENT _row_hash
    -- values and both survive here as a genuine grain conflict --
    -- surfaced by tests/dbt/assert_no_medical_claim_grain_conflicts.sql
    -- and by models/final/schema.yml's primary-key uniqueness test,
    -- never silently resolved by picking one.
    select
        *,
        row_number() over (
            partition by claim_id, claim_line_number, data_source, _row_hash
            order by _loaded_at, _source_row_number
        ) as _duplicate_rank
    from staging

),

deduped_unique as (

    select * from deduped where _duplicate_rank = 1

),

identity_resolved as (

    select
        d.*,
        coalesce(d.person_id, cw.person_id)     as resolved_person_id,
        coalesce(d.member_id, d.member_crosswalk_key) as resolved_member_id,
        (d.person_id is not null or cw.person_id is not null) as matched_member
    from deduped_unique d
    left join {{ ref('int_member_crosswalk') }} cw on cw.member_key = d.member_crosswalk_key

),

dates_aggregated as (

    -- claim_start_date/claim_end_date: use the source's own
    -- claim-header-level value when supplied (the existing "tuva"
    -- source); otherwise derive it by aggregating this claim_id's own
    -- lines (the vendor source, which supplies only a per-line service
    -- date pair -- see stg_medical_claim.sql). This is a genuinely
    -- cross-row rule (a window function over every line sharing the
    -- same claim_id), which is exactly why it lives here and not in
    -- staging.
    select
        i.*,
        coalesce(i.claim_start_date, min(i.claim_line_start_date) over (partition by i.claim_id, i.data_source)) as resolved_claim_start_date,
        coalesce(i.claim_end_date, max(i.claim_line_end_date) over (partition by i.claim_id, i.data_source))     as resolved_claim_end_date
    from identity_resolved i

),

payer_plan_resolved as (

    -- payer/plan derivation for a claim line whose source does not
    -- carry payer/plan directly (the vendor source -- see
    -- stg_medical_claim.sql's header): inherit it from the eligibility
    -- coverage span active on the claim's own service date, for the
    -- same resolved person and data_source. Deterministic when more
    -- than one span could apply (e.g. genuinely concurrent medical/
    -- dental coverage): the most recently started matching span wins,
    -- tie-broken by payer/plan text -- never a random pick.
    select
        d.*,
        coalesce(d.payer, elig.payer) as resolved_payer,
        coalesce(d.plan, elig.plan)   as resolved_plan
    from dates_aggregated d
    left join lateral (
        select e.payer, e.plan
        from {{ ref('int_eligibility_spans') }} e
        where d.payer is null
          and e.person_id = d.resolved_person_id
          and e.data_source = d.data_source
          and coalesce(d.resolved_claim_start_date, d.claim_line_start_date) is not null
          and coalesce(d.resolved_claim_start_date, d.claim_line_start_date) >= e.enrollment_start_date
          and (
              e.enrollment_end_date is null
              or coalesce(d.resolved_claim_start_date, d.claim_line_start_date) <= e.enrollment_end_date
          )
        order by e.enrollment_start_date desc, e.payer, e.plan
        limit 1
    ) elig on true

),

claim_typed as (

    select
        p.*,
        {{ derive_claim_type('p.source_claim_type_hint', 'p.claim_form_code', 'p.bill_type_code', 'p.place_of_service_code') }} as resolved_claim_type
    from payer_plan_resolved p

),

diagnoses_normalized as (

    -- Diagnosis normalization (decision 4): dedup exact-repeat codes
    -- across diag_cd_1..3 while preserving source column position as
    -- sequence (position 1 always primary), compacting the surviving
    -- codes into consecutive positions. array_remove(..., NULL)
    -- preserves relative order while dropping dropped/duplicate
    -- positions, which is exactly the "first occurrence keeps its
    -- sequence position" rule -- entirely row-local (one row's own 3
    -- diagnosis columns), not a cross-row join.
    select
        c.*,
        (
            array_remove(
                array[
                    c.diag_cd_1,
                    case when c.diag_cd_2 is not null and c.diag_cd_2 is distinct from c.diag_cd_1
                        then c.diag_cd_2 end,
                    case when c.diag_cd_3 is not null
                            and c.diag_cd_3 is distinct from c.diag_cd_1
                            and c.diag_cd_3 is distinct from c.diag_cd_2
                        then c.diag_cd_3 end
                ],
                null
            )
        ) as _diagnosis_codes
    from claim_typed c

),

procedures_normalized as (

    -- Procedure normalization (decision 4): only 2 source positions, so
    -- deduplication never needs to compact a later position forward --
    -- dropping position 2 as a duplicate of position 1 simply leaves
    -- position 2 (code and date together) NULL.
    select
        n.*,
        n.proc_cd_1 as _procedure_code_1,
        n.proc_dt_1 as _procedure_date_1,
        case when n.proc_cd_2 is not null and n.proc_cd_2 is distinct from n.proc_cd_1
            then n.proc_cd_2 end as _procedure_code_2,
        case when n.proc_cd_2 is not null and n.proc_cd_2 is distinct from n.proc_cd_1
            then n.proc_dt_2 end as _procedure_date_2
    from diagnoses_normalized n

),

lifecycle_links as (

    -- Adjustment lineage (decision 2): every claim_id referenced by an
    -- adjustment (status 7)'s orig_clm_id, scoped to data_source (two
    -- sources could reuse the same claim_id numbering).
    select distinct
        orig_clm_id as claim_id,
        data_source,
        claim_id    as superseded_by_claim_id
    from procedures_normalized
    where clm_status_code = '7'
      and orig_clm_id is not null

),

lifecycle_flagged as (

    select
        p.*,
        (l.claim_id is not null and p.clm_status_code = '1') as is_superseded,
        l.superseded_by_claim_id,
        case
            when p.clm_status_code = '1' then 'original'
            when p.clm_status_code = '7' then 'adjustment'
            when p.clm_status_code = '8' then 'void'
            when p.clm_status_code is null then 'unclassified'
            else 'unrecognized_status_' || p.clm_status_code
        end as lifecycle_action
    from procedures_normalized p
    left join lifecycle_links l on l.claim_id = p.claim_id and l.data_source = p.data_source

)

select
    claim_id,
    claim_line_number,
    resolved_claim_type    as claim_type,
    resolved_person_id     as person_id,
    resolved_member_id     as member_id,
    matched_member,
    resolved_payer         as payer,
    resolved_plan          as plan,

    resolved_claim_start_date as claim_start_date,
    resolved_claim_end_date   as claim_end_date,
    claim_line_start_date,
    claim_line_end_date,
    admission_date,
    discharge_date,
    paid_date,

    admit_source_code,
    admit_type_code,
    discharge_disposition_code,
    place_of_service_code,
    bill_type_code,
    revenue_center_code,

    drg_code_type,
    drg_code,

    service_unit_quantity,
    hcpcs_code,
    hcpcs_modifier_1,
    hcpcs_modifier_2,

    rendering_npi,
    billing_npi,
    facility_npi,

    paid_amount,
    allowed_amount,
    charge_amount,
    coinsurance_amount,
    copayment_amount,
    deductible_amount,

    coalesce(diag_type, case when array_length(_diagnosis_codes, 1) > 0 then 'UNKNOWN_CODE_TYPE' end) as diagnosis_code_type,
    _diagnosis_codes[1] as diagnosis_code_1,
    _diagnosis_codes[2] as diagnosis_code_2,
    _diagnosis_codes[3] as diagnosis_code_3,

    coalesce(proc_type, case when _procedure_code_1 is not null then 'UNKNOWN_CODE_TYPE' end) as procedure_code_type,
    _procedure_code_1 as procedure_code_1,
    _procedure_date_1 as procedure_date_1,
    _procedure_code_2 as procedure_code_2,
    _procedure_date_2 as procedure_date_2,

    clm_status_code,
    orig_clm_id,
    lifecycle_action,
    is_superseded,
    superseded_by_claim_id,

    data_source,
    _snapshot_id,
    _source_row_number,
    _loaded_at,
    _row_hash

from lifecycle_flagged
