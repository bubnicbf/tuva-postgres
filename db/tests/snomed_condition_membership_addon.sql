-- db/tests/snomed_condition_membership_addon.sql
-- Expects :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

WITH missing AS (
  SELECT DISTINCT c.normalized_code AS snomed
  FROM condition c
  WHERE c.normalized_code IS NOT NULL
    AND btrim(c.normalized_code) <> ''
    AND c.normalized_code_type IN ('SNOMED','SNOMED-CT')
    AND NOT EXISTS (
      SELECT 1 FROM :"terminology_schema".snomed_ct t
      WHERE t.snomed_ct = c.normalized_code
    )
)
SELECT 'condition_snomed_unknown_codes' AS test,
       (SELECT COUNT(*) FROM missing) = 0 AS pass,
       (SELECT COUNT(*) FROM missing)     AS unknown_code_count;
