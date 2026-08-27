-- Data-quality singular test (docs/CLAIMS_MAPPING_DECISIONS.md decision
-- 7; executable reference: src/tuva_ingest/claims_mapping.
-- find_span_overlaps). A dbt test FAILS when this query returns any
-- rows -- proves models/intermediate/int_eligibility_spans.sql's merge
-- algorithm leaves no two consolidated spans in the same
-- (person_id, payer, plan, coverage_type, data_source) partition still
-- overlapping. Self-join, excluding a span against itself and
-- de-duplicating symmetric pairs (a < b) so each true overlap is
-- reported once.

with spans as (

    select * from {{ ref('int_eligibility_spans') }}

)

select
    a.person_id,
    a.payer,
    a.plan,
    a.coverage_type,
    a.data_source,
    a.enrollment_start_date as a_start,
    a.enrollment_end_date   as a_end,
    b.enrollment_start_date as b_start,
    b.enrollment_end_date   as b_end
from spans a
join spans b
    on a.person_id = b.person_id
   and a.payer = b.payer
   and a.plan = b.plan
   and a.coverage_type = b.coverage_type
   and a.data_source = b.data_source
   and a.enrollment_start_date < b.enrollment_start_date
where a.enrollment_end_date is not null
  and b.enrollment_start_date <= a.enrollment_end_date
