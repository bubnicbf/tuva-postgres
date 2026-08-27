{{
  config(
    materialized='view'
  )
}}

-- Row-level (not yet span-consolidated) eligibility resolution: member
-- identity resolution via models/intermediate/int_member_crosswalk.sql,
-- exact-duplicate-delivery collapse, and invalid-range detection. Span
-- consolidation (overlap/adjacency merging) happens one layer further
-- in models/intermediate/int_eligibility_spans.sql, which consumes only
-- this model's resolved, valid, matched rows.
--
-- This is the auditability layer for eligibility (see README.md
-- "Architecture"): every source row survives here, flagged, so a
-- quarantined or unmatched row is never silently dropped -- only
-- filtered out one layer later when building the consolidated spans
-- that actually feed models/final/eligibility.sql.

with staging as (

    select * from {{ ref('stg_eligibility') }}

),

crosswalk as (

    select * from {{ ref('int_member_crosswalk') }}

),

resolved as (

    select
        s._snapshot_id,
        s._source_row_number,
        s._loaded_at,
        s._row_hash,

        -- Identity resolution (docs/CLAIMS_MAPPING_DECISIONS.md
        -- decision 5): a direct source person_id (the existing "tuva"
        -- source) always wins; otherwise resolve member_crosswalk_key
        -- through the crosswalk (the vendor source). Never both -- the
        -- two source vocabularies are mutually exclusive per row (see
        -- stg_eligibility.sql).
        coalesce(s.person_id, cw.person_id)                as resolved_person_id,
        s.person_id                                        as source_person_id,
        s.member_id,
        s.subscriber_id,
        s.member_crosswalk_key,
        (s.person_id is not null or cw.person_id is not null) as matched_member,

        s.coverage_type,
        s.birth_date,
        s.death_date,
        s.enrollment_start_date,
        s.enrollment_end_date,
        s.payer,
        s.payer_type,
        s.plan,
        s.original_reason_entitlement_code,
        s.dual_status_code,
        s.medicare_status_code,
        s.group_id,
        s.group_name,
        s.data_source,

        -- member_id fallback for the vendor source, which has no
        -- distinct payer-assigned member_id field beyond member_key
        -- itself (docs/CLAIMS_MAPPING.csv: "member_key -- Vendor-
        -- assigned member ID"). The Input Layer contract requires
        -- member_id NOT NULL; using the vendor's own member_key here is
        -- a documented decision, not an invented value -- see docs/
        -- CLAIMS_MAPPING_DECISIONS.md "Member identity".
        coalesce(s.member_id, s.member_crosswalk_key)      as resolved_member_id,

        -- Range validity (docs/CLAIMS_MAPPING_DECISIONS.md decision 7):
        -- span_start_dt is required; an end date before the start date
        -- is invalid and must be surfaced, never silently accepted or
        -- auto-corrected.
        case
            when s.enrollment_start_date is null then 'missing_or_unparseable_start_date'
            when s.enrollment_end_date is not null and s.enrollment_end_date < s.enrollment_start_date
                then 'end_date_before_start_date'
            when s.person_id is null and cw.person_id is null then 'unmatched_member_key'
            else null
        end                                                 as quarantine_reason

    from staging s
    left join crosswalk cw on cw.member_key = s.member_crosswalk_key

),

deduped as (

    -- Exact-duplicate-delivery collapse (docs/CLAIMS_MAPPING_DECISIONS.md
    -- decision 1's dedup step, applied to eligibility the same way it
    -- applies to claims): a byte-identical repeat of the same span
    -- collapses to one row, keeping the earliest-delivered copy via
    -- stable source metadata (never a nondeterministic order). Rows
    -- that share identity/dates but differ elsewhere are NOT exact
    -- duplicates and both survive here -- they are handled as distinct
    -- spans, or surfaced as overlaps, by
    -- models/intermediate/int_eligibility_spans.sql.
    select
        *,
        row_number() over (
            partition by _row_hash
            order by _loaded_at, _source_row_number
        ) as _duplicate_rank
    from resolved

)

select
    resolved_person_id,
    source_person_id,
    resolved_member_id,
    subscriber_id,
    member_crosswalk_key,
    matched_member,
    coverage_type,
    birth_date,
    death_date,
    enrollment_start_date,
    enrollment_end_date,
    payer,
    payer_type,
    plan,
    original_reason_entitlement_code,
    dual_status_code,
    medicare_status_code,
    group_id,
    group_name,
    data_source,
    quarantine_reason,
    (quarantine_reason is not null)   as is_quarantined,
    _snapshot_id,
    _source_row_number,
    _loaded_at,
    _row_hash
from deduped
where _duplicate_rank = 1
