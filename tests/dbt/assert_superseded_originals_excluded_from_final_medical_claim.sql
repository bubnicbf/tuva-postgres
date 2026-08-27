-- Prevents double-counting (docs/CLAIMS_MAPPING_DECISIONS.md decision
-- 2, "adjustment replaces original"): a claim line flagged
-- is_superseded in models/intermediate/int_medical_claim_lines.sql must
-- never also appear in models/final/medical_claim.sql. A dbt test FAILS
-- when this query returns any rows.

with superseded as (

    select claim_id, claim_line_number, data_source
    from {{ ref('int_medical_claim_lines') }}
    where is_superseded

)

select f.*
from {{ ref('medical_claim') }} f
join superseded s
    on s.claim_id = f.claim_id
   and s.claim_line_number = f.claim_line_number
   and s.data_source = f.data_source
