-- db/tests/icd_membership_addon.sql
-- Soft checks: selected tables’ ICD codes exist in corresponding terminology.
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- A) Encounter primary diagnosis
WITH enc AS (
  SELECT primary_diagnosis_code_type AS ct, primary_diagnosis_code AS code
  FROM encounter
  WHERE primary_diagnosis_code IS NOT NULL AND BTRIM(primary_diagnosis_code) <> ''
    AND primary_diagnosis_code_type IS NOT NULL
)
SELECT 'enc_primary_dx_membership' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS unknown_count
FROM (
  SELECT 1
  FROM enc e
  LEFT JOIN :"terminology_schema".icd_10_cm t10
    ON e.ct IN ('ICD-10-CM','ICD10CM') AND e.code = t10.icd_10_cm
  LEFT JOIN :"terminology_schema".icd_9_cm t9
    ON e.ct IN ('ICD-9-CM','ICD9CM')   AND e.code = t9.icd_9_cm
  WHERE (e.ct IN ('ICD-10-CM','ICD10CM') AND t10.icd_10_cm IS NULL)
     OR (e.ct IN ('ICD-9-CM','ICD9CM')   AND t9.icd_9_cm  IS NULL)
) x;

-- B) Condition normalized codes
WITH con AS (
  SELECT normalized_code_type AS ct, normalized_code AS code
  FROM condition
  WHERE normalized_code IS NOT NULL AND BTRIM(normalized_code) <> ''
    AND normalized_code_type IS NOT NULL
)
SELECT 'condition_norm_icd_membership' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS unknown_count
FROM (
  SELECT 1
  FROM con c
  LEFT JOIN :"terminology_schema".icd_10_cm t10
    ON c.ct IN ('ICD-10-CM','ICD10CM') AND c.code = t10.icd_10_cm
  LEFT JOIN :"terminology_schema".icd_9_cm t9
    ON c.ct IN ('ICD-9-CM','ICD9CM')   AND c.code = t9.icd_9_cm
  WHERE (c.ct IN ('ICD-10-CM','ICD10CM') AND t10.icd_10_cm IS NULL)
     OR (c.ct IN ('ICD-9-CM','ICD9CM')   AND t9.icd_9_cm  IS NULL)
) x;
