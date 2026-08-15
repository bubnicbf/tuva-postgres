-- db/tests/patient_gender_addon.sql
-- Soft check: patient.gender must exist in terminology.gender.
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

WITH missing AS (
  SELECT DISTINCT p.gender AS gender
  FROM patient p
  LEFT JOIN :"terminology_schema".gender g
    ON p.gender = g.gender
  WHERE p.gender IS NOT NULL AND BTRIM(p.gender) <> ''
    AND g.gender IS NULL
)
SELECT 'patient_gender_unknown_codes' AS test,
       (SELECT COUNT(*) FROM missing) = 0 AS pass,
       (SELECT COUNT(*) FROM missing)     AS unknown_code_count;
