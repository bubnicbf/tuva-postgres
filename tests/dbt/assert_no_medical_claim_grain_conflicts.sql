-- Data-quality singular test (docs/CLAIMS_MAPPING_DECISIONS.md decision
-- 1; executable reference: src/tuva_ingest/claims_mapping.
-- find_grain_conflicts). models/intermediate/int_medical_claim_lines.sql
-- collapses byte-identical duplicate deliveries but deliberately never
-- resolves two rows that share (claim_id, claim_line_number,
-- data_source) with genuinely DIFFERENT content -- both survive, as a
-- surfaced conflict, never an arbitrarily-picked winner. This test
-- makes that conflict visible as a failing dbt test (rather than only
-- as an eventual final-layer primary-key failure with no direct
-- diagnostic) by counting distinct content fingerprints per grain key.

select
    claim_id,
    claim_line_number,
    data_source,
    count(distinct _row_hash) as distinct_versions
from {{ ref('int_medical_claim_lines') }}
group by claim_id, claim_line_number, data_source
having count(distinct _row_hash) > 1
