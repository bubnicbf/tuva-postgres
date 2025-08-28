-- db/tests/appointment_type_codes_addon.sql
-- Soft check: distinct appointment type codes must exist in terminology.appointment_type.
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- 1) Source appointment type codes not found (distinct)
WITH missing AS (
  SELECT DISTINCT a.source_appointment_type_code AS code
  FROM appointment a
  LEFT JOIN :"terminology_schema".appointment_type t
    ON a.source_appointment_type_code = t.code
  WHERE a.source_appointment_type_code IS NOT NULL
    AND t.code IS NULL
)
SELECT 'appt_source_type_unknown_codes' AS test,
       (SELECT COUNT(*) FROM missing) = 0 AS pass,
       (SELECT COUNT(*) FROM missing)     AS unknown_code_count;

-- 2) Normalized appointment type codes not found (distinct)
WITH missing AS (
  SELECT DISTINCT a.normalized_appointment_type_code AS code
  FROM appointment a
  LEFT JOIN :"terminology_schema".appointment_type t
    ON a.normalized_appointment_type_code = t.code
  WHERE a.normalized_appointment_type_code IS NOT NULL
    AND t.code IS NULL
)
SELECT 'appt_normalized_type_unknown_codes' AS test,
       (SELECT COUNT(*) FROM missing) = 0 AS pass,
       (SELECT COUNT(*) FROM missing)     AS unknown_code_count;
