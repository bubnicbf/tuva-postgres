-- Uses :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- 1) encounter: admit_type_code should exist in terminology (when present)
SELECT 'encounter_admit_type_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_count
FROM encounter e
LEFT JOIN :"terminology_schema".admit_type t
  ON e.admit_type_code = t.admit_type_code
WHERE e.admit_type_code IS NOT NULL
  AND t.admit_type_code IS NULL;

-- 2) medical_claim: admit_type_code should exist in terminology (when present)
SELECT 'medical_claim_admit_type_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_count
FROM medical_claim m
LEFT JOIN :"terminology_schema".admit_type t
  ON m.admit_type_code = t.admit_type_code
WHERE m.admit_type_code IS NOT NULL
  AND t.admit_type_code IS NULL;

-- 3) encounter: (soft) description mismatch with terminology
SELECT 'encounter_admit_type_desc_mismatch' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS mismatch_count
FROM encounter e
JOIN :"terminology_schema".admit_type t
  ON e.admit_type_code = t.admit_type_code
WHERE e.admit_type_description IS NOT NULL
  AND t.admit_type_description IS NOT NULL
  AND e.admit_type_description <> t.admit_type_description;

-- 4) medical_claim: (soft) description mismatch with terminology
SELECT 'medical_claim_admit_type_desc_mismatch' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS mismatch_count
FROM medical_claim m
JOIN :"terminology_schema".admit_type t
  ON m.admit_type_code = t.admit_type_code
WHERE m.admit_type_description IS NOT NULL
  AND t.admit_type_description IS NOT NULL
  AND m.admit_type_description <> t.admit_type_description;
