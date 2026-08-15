-- db/tests/observation_loinc_addon.sql
-- Soft check: observation.normalized_code must exist in terminology.loinc when normalized_code_type='LOINC'.
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- Distinct unknown LOINC codes
WITH missing AS (
  SELECT DISTINCT o.normalized_code AS loinc
  FROM observation o
  LEFT JOIN :"terminology_schema".loinc t
    ON o.normalized_code = t.loinc
  WHERE o.normalized_code_type = 'LOINC'
    AND o.normalized_code IS NOT NULL
    AND btrim(o.normalized_code) <> ''
    AND t.loinc IS NULL
)
SELECT 'obs_loinc_unknown_codes' AS test,
       (SELECT COUNT(*) FROM missing) = 0 AS pass,
       (SELECT COUNT(*) FROM missing)     AS unknown_code_count;

-- Row-level occurrences using unknown LOINC codes (scale visibility)
SELECT 'obs_loinc_unknown_rows' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*)     AS unknown_row_count
FROM observation o
LEFT JOIN :"terminology_schema".loinc t
  ON o.normalized_code = t.loinc
WHERE o.normalized_code_type = 'LOINC'
  AND o.normalized_code IS NOT NULL
  AND btrim(o.normalized_code) <> ''
  AND t.loinc IS NULL;
