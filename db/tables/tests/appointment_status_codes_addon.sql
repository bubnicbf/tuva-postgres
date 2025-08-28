-- db/tests/appointment_status_codes_addon.sql
-- Soft check: distinct status codes must exist in terminology.appointment_status.
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- 1) Source status codes not found (distinct)
WITH missing AS (
  SELECT DISTINCT a.source_status AS code
  FROM appointment a
  LEFT JOIN :"terminology_schema".appointment_status s
    ON a.source_status = s.code
  WHERE a.source_status IS NOT NULL
    AND s.code IS NULL
)
SELECT 'appt_source_status_unknown_codes' AS test,
       (SELECT COUNT(*) FROM missing) = 0 AS pass,
       (SELECT COUNT(*) FROM missing)     AS unknown_code_count;

-- 2) Normalized status codes not found (distinct)
WITH missing AS (
  SELECT DISTINCT a.normalized_status AS code
  FROM appointment a
  LEFT JOIN :"terminology_schema".appointment_status s
    ON a.normalized_status = s.code
  WHERE a.normalized_status IS NOT NULL
    AND s.code IS NULL
)
SELECT 'appt_normalized_status_unknown_codes' AS test,
       (SELECT COUNT(*) FROM missing) = 0 AS pass,
       (SELECT COUNT(*) FROM missing)     AS unknown_code_count;
