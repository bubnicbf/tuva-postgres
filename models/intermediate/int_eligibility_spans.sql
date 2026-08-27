{{
  config(
    materialized='view'
  )
}}

-- Eligibility-span consolidation (docs/CLAIMS_MAPPING_DECISIONS.md
-- decision 7; executable reference: src/tuva_ingest/claims_mapping.
-- consolidate_eligibility). Consumes only matched, valid rows from
-- models/intermediate/int_eligibility_resolved.sql -- unmatched
-- member_key values and invalid (end < start) ranges were already
-- flagged there and are excluded here by design (see that model's
-- `is_quarantined` column; a singular test asserts a quarantined row
-- never reaches this model -- tests/dbt/assert_quarantined_eligibility_
-- never_reaches_spans.sql).
--
-- Partition key: (person_id, payer, plan, coverage_type, data_source).
-- The first four match decision 7 exactly (coverage_type has no Input
-- Layer destination column -- it exists purely so concurrent
-- medical/dental coverage under the same payer/plan is never merged
-- into one span); `data_source` is this implementation's own necessary
-- extension, since this connector ingests more than one source and two
-- sources' spans for the "same" person must never merge into one
-- (matches the same reasoning models/final/schema.yml already applies
-- to the primary-key grain).
--
-- Algorithm per partition: collapse exact-duplicate spans (identical
-- start/end), then merge closed spans (a populated end date) that
-- overlap or are adjacent (next span's start_date <= prior span's
-- end_date + 1 day) using a standard "gaps and islands" window-function
-- merge -- ordered deterministically by (enrollment_start_date,
-- enrollment_end_date), never by an unstable/nondeterministic order.
-- Open-ended spans (a NULL end date -- ongoing coverage) are preserved
-- exactly as-is and are never merged with anything, including each
-- other -- matching claims_mapping.consolidate_eligibility precisely
-- (its own docstring: "nothing can be adjacent to an open end").
--
-- Attributes that are not part of the partition key or the merged span
-- boundary itself (member_id, payer_type, group_id/group_name,
-- original_reason_entitlement_code, dual_status_code,
-- medicare_status_code, birth_date, death_date) are taken from the
-- earliest-starting contributing row in each merged group, tie-broken
-- by (_loaded_at, _source_row_number) -- deterministic window ordering
-- over stable source metadata, never a random/arbitrary pick. This
-- resolves a gap the Python reference implementation's own
-- EligibilitySpan leaves open (it drops member identity entirely once
-- spans are merged), because the Input Layer contract requires a
-- non-null member_id per eligibility row and the documented partition
-- key does not carry member-key granularity.

with eligible as (

    select *
    from {{ ref('int_eligibility_resolved') }}
    where not is_quarantined

),

partitioned as (

    select
        *,
        coalesce(payer, '')         as _partition_payer,
        coalesce(plan, '')          as _partition_plan,
        coalesce(coverage_type, '') as _partition_coverage_type
    from eligible

),

deduped as (

    -- Exact-duplicate span collapse: same partition + same start/end
    -- (open or closed) collapses to the earliest-delivered copy.
    select
        *,
        row_number() over (
            partition by
                resolved_person_id, _partition_payer, _partition_plan, _partition_coverage_type, data_source,
                enrollment_start_date, enrollment_end_date
            order by _loaded_at, _source_row_number
        ) as _duplicate_rank
    from partitioned

),

deduped_unique as (

    select * from deduped where _duplicate_rank = 1

),

closed as (

    select * from deduped_unique where enrollment_end_date is not null

),

open_ended as (

    select * from deduped_unique where enrollment_end_date is null

),

closed_with_lag as (

    select
        *,
        lag(enrollment_end_date) over (
            partition by resolved_person_id, _partition_payer, _partition_plan, _partition_coverage_type, data_source
            order by enrollment_start_date, enrollment_end_date
        ) as _prev_end_date
    from closed

),

closed_flagged as (

    select
        *,
        case
            when _prev_end_date is null then 1
            when enrollment_start_date > _prev_end_date + 1 then 1
            else 0
        end as _new_island
    from closed_with_lag

),

closed_islands as (

    select
        *,
        sum(_new_island) over (
            partition by resolved_person_id, _partition_payer, _partition_plan, _partition_coverage_type, data_source
            order by enrollment_start_date, enrollment_end_date
            rows between unbounded preceding and current row
        ) as _island_id
    from closed_flagged

),

closed_merged as (

    select
        resolved_person_id                                                                   as person_id,
        _partition_payer                                                                      as payer,
        _partition_plan                                                                       as plan,
        _partition_coverage_type                                                              as coverage_type,
        data_source,
        min(enrollment_start_date)                                                            as enrollment_start_date,
        max(enrollment_end_date)                                                               as enrollment_end_date,
        (array_agg(resolved_member_id order by enrollment_start_date, _loaded_at, _source_row_number))[1]   as member_id,
        (array_agg(subscriber_id order by enrollment_start_date, _loaded_at, _source_row_number))[1]        as subscriber_id,
        (array_agg(payer_type order by enrollment_start_date, _loaded_at, _source_row_number))[1]           as payer_type,
        (array_agg(original_reason_entitlement_code order by enrollment_start_date, _loaded_at, _source_row_number))[1]
                                                                                                as original_reason_entitlement_code,
        (array_agg(dual_status_code order by enrollment_start_date, _loaded_at, _source_row_number))[1]     as dual_status_code,
        (array_agg(medicare_status_code order by enrollment_start_date, _loaded_at, _source_row_number))[1] as medicare_status_code,
        (array_agg(group_id order by enrollment_start_date, _loaded_at, _source_row_number))[1]             as group_id,
        (array_agg(group_name order by enrollment_start_date, _loaded_at, _source_row_number))[1]           as group_name,
        (array_agg(birth_date order by enrollment_start_date, _loaded_at, _source_row_number))[1]           as birth_date,
        (array_agg(death_date order by enrollment_start_date, _loaded_at, _source_row_number))[1]           as death_date
    from closed_islands
    group by resolved_person_id, _partition_payer, _partition_plan, _partition_coverage_type, data_source, _island_id

),

open_final as (

    select
        resolved_person_id            as person_id,
        _partition_payer              as payer,
        _partition_plan               as plan,
        _partition_coverage_type      as coverage_type,
        data_source,
        enrollment_start_date,
        enrollment_end_date,
        resolved_member_id            as member_id,
        subscriber_id,
        payer_type,
        original_reason_entitlement_code,
        dual_status_code,
        medicare_status_code,
        group_id,
        group_name,
        birth_date,
        death_date
    from open_ended

),

unioned as (

    select * from closed_merged
    union all
    select * from open_final

)

select
    person_id,
    member_id,
    subscriber_id,
    payer,
    plan,
    coverage_type,
    payer_type,
    birth_date,
    death_date,
    enrollment_start_date,
    enrollment_end_date,
    original_reason_entitlement_code,
    dual_status_code,
    medicare_status_code,
    group_id,
    group_name,
    data_source
from unioned
