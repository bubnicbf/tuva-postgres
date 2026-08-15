-- Uses :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- If admit_type_code indicates Newborn and we have a known admit_source_code,
-- ensure terminology has a newborn_description populated.
SELECT 'admit_newborn_term_present' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS missing_newborn_desc_count
FROM encounter e
LEFT JOIN :"terminology_schema".admit_source a
  ON e.admit_source_code = a.admit_source_code
WHERE e.admit_type_code IN ('4','04')
  AND e.admit_source_code IS NOT NULL
  AND (a.admit_source_code IS NULL OR a.newborn_description IS NULL);
