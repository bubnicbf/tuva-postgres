-- db/tests/discharge_disposition_addon.sql
-- Soft checks: discharge_disposition_code should exist in terminology.discharge_disposition.
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- 1) Encounter: distinct codes not found in terminology
WITH missing AS (
  SELECT DISTINCT e.discharge_disposition_code AS code
  FROM encounter e
  LEFT JOIN :"terminology_schema".discharge_disposition t
    ON e.discharge_disposition_code = t.discharge_disposition_code
  WHERE e.discharge_disposition_code IS NOT NULL
    AND btrim(e.discharge_disposition_code) <> ''
    AND t.discharge_disposition_code IS NULL
)
SELECT 'encounter_discharge_disposition_unknown_codes' AS test,
       (SELECT COUNT(*) FROM missing) = 0 AS pass,
       (SELECT COUNT(*) FROM missing)     AS unknown_code_count;

-- 2) Medical claim: distinct codes not found in terminology
WITH missing AS (
  SELECT DISTINCT m.discharge_disposition_code AS code
  FROM medical_claim m
  LEFT JOIN :"terminology_schema".discharge_disposition t
    ON m.discharge_disposition_code = t.discharge_disposition_code
  WHERE m.discharge_disposition_code IS NOT NULL
    AND btrim(m.discharge_disposition_code) <> ''
    AND t.discharge_disposition_code IS NULL
)
SELECT 'mc_discharge_disposition_unknown_codes' AS test,
       (SELECT COUNT(*) FROM missing) = 0 AS pass,
       (SELECT COUNT(*) FROM missing)     AS unknown_code_count;
