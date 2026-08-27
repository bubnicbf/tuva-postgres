-- Financial reconciliation (docs/CLAIMS_MAPPING_DECISIONS.md decision
-- 6; executable reference: src/tuva_ingest/claims_mapping.
-- reconciliation_violations): |paid_amount| <= |allowed_amount| <=
-- |charge_amount| whenever all three are populated, on every medical
-- claim line -- by absolute value specifically so a void's negative
-- triple still has to satisfy the same magnitude ordering as its
-- original. Rows with any of the three NULL are excluded (not yet
-- adjudicated -- not a reconciliation question).

select claim_id, claim_line_number, data_source, paid_amount, allowed_amount, charge_amount
from {{ ref('int_medical_claim_lines') }}
where paid_amount is not null
  and allowed_amount is not null
  and charge_amount is not null
  and not (abs(paid_amount) <= abs(allowed_amount) and abs(allowed_amount) <= abs(charge_amount))
