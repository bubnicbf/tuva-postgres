-- db/tests/ms_drg_addon.sql
-- Soft checks for MS-DRG membership and deprecation.
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- 1) Encounter: distinct DRG codes not found in terminology
WITH enc_codes AS (
  SELECT DISTINCT btrim(drg_code) AS drg
  FROM encounter
  WHERE drg_code IS NOT NULL AND btrim(drg_code) <> ''
)
SELECT 'encounter_drg_unknown_codes' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*)     AS unknown_code_count
FROM enc_codes e
LEFT JOIN :"terminology_schema".ms_drg t
  ON e.drg = t.ms_drg_code
WHERE t.ms_drg_code IS NULL;

-- 2) Medical claim: distinct DRG codes not found in terminology
WITH mc_codes AS (
  SELECT DISTINCT btrim(drg_code) AS drg
  FROM medical_claim
  WHERE drg_code IS NOT NULL AND btrim(drg_code) <> ''
)
SELECT 'mc_drg_unknown_codes' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*)     AS unknown_code_count
FROM mc_codes m
LEFT JOIN :"terminology_schema".ms_drg t
  ON m.drg = t.ms_drg_code
WHERE t.ms_drg_code IS NULL;

-- 3) Encounter: rows using DRGs marked deprecated=1
SELECT 'encounter_drg_deprecated_rows' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*)     AS deprecated_row_count
FROM encounter e
JOIN :"terminology_schema".ms_drg t
  ON btrim(e.drg_code) = t.ms_drg_code
WHERE e.drg_code IS NOT NULL AND btrim(e.drg_code) <> ''
  AND t.deprecated = 1;

-- 4) Medical claim: rows using DRGs marked deprecated=1
SELECT 'mc_drg_deprecated_rows' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*)     AS deprecated_row_count
FROM medical_claim m
JOIN :"terminology_schema".ms_drg t
  ON btrim(m.drg_code) = t.ms_drg_code
WHERE m.drg_code IS NOT NULL AND btrim(m.drg_code) <> ''
  AND t.deprecated = 1;
