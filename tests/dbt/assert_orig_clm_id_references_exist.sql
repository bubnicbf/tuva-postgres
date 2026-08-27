-- Surfaces a broken/orphaned orig_clm_id reference (a required
-- referential-integrity check this task calls out explicitly): every
-- non-null orig_clm_id on an adjustment (status 7) or void (status 8)
-- line must reference a claim_id that actually exists elsewhere in the
-- same data_source. Implemented as a custom singular test rather than
-- the generic `relationships` test because the reference is scoped by
-- (claim_id, data_source) together -- a composite key the generic
-- single-column `relationships` test cannot express.

with lines as (

    select * from {{ ref('int_medical_claim_lines') }}

),

known_claim_ids as (

    select distinct claim_id, data_source from lines

)

select l.claim_id, l.claim_line_number, l.data_source, l.orig_clm_id, l.clm_status_code
from lines l
left join known_claim_ids k
    on k.claim_id = l.orig_clm_id
   and k.data_source = l.data_source
where l.orig_clm_id is not null
  and k.claim_id is null
