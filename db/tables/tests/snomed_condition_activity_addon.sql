-- db/tests/snomed_condition_activity_addon.sql
-- Expects :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

SELECT 'condition_snomed_inactive_rows' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*)     AS inactive_row_count
FROM condition c
JOIN :"terminology_schema".snomed_ct t
  ON c.normalized_code_type IN ('SNOMED','SNOMED-CT')
 AND c.normalized_code = t.snomed_ct
WHERE COALESCE(t.is_active,'1') = '0';
