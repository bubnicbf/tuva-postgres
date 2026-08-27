-- Prevents double-counting the other way (docs/
-- CLAIMS_MAPPING_DECISIONS.md decision 2, "void cancels original"): for
-- every void claim (status 8) whose orig_clm_id references a
-- not-superseded original still present in models/final/
-- medical_claim.sql, the void's total paid_amount plus the original's
-- total paid_amount must net to zero -- the documented signed-amount
-- cancellation behavior, proving the void's negative amount actually
-- offsets its original rather than merely coexisting with it. A dbt
-- test FAILS when this returns any rows (tolerance: one cent, to allow
-- for per-line rounding across multiple lines).

with voids as (

    select claim_id, orig_clm_id, data_source, sum(paid_amount) as void_total
    from {{ ref('medical_claim') }} mc
    join {{ ref('int_medical_claim_lines') }} lines
        on lines.claim_id = mc.claim_id
       and lines.claim_line_number = mc.claim_line_number
       and lines.data_source = mc.data_source
    where lines.clm_status_code = '8'
      and lines.orig_clm_id is not null
    group by claim_id, orig_clm_id, data_source

),

originals as (

    select claim_id, data_source, sum(paid_amount) as original_total
    from {{ ref('medical_claim') }}
    group by claim_id, data_source

)

select v.claim_id, v.orig_clm_id, v.data_source, v.void_total, o.original_total
from voids v
join originals o
    on o.claim_id = v.orig_clm_id
   and o.data_source = v.data_source
where abs(coalesce(v.void_total, 0) + coalesce(o.original_total, 0)) > 0.01
