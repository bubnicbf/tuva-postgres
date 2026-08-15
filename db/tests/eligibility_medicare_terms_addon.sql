-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- Dual eligibility membership
SELECT 'elig_dual_status_known' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*)     AS unknown_code_count
FROM (
  SELECT DISTINCT dual_status_code AS code
  FROM eligibility
  WHERE dual_status_code IS NOT NULL AND btrim(dual_status_code) <> ''
) s
LEFT JOIN :"terminology_schema".medicare_dual_eligibility t
  ON s.code = t.dual_status_code
WHERE t.dual_status_code IS NULL;

-- OREC membership
SELECT 'elig_orec_known' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*)     AS unknown_code_count
FROM (
  SELECT DISTINCT original_reason_entitlement_code AS code
  FROM eligibility
  WHERE original_reason_entitlement_code IS NOT NULL AND btrim(original_reason_entitlement_code) <> ''
) s
LEFT JOIN :"terminology_schema".medicare_orec t
  ON s.code = t.original_reason_entitlement_code
WHERE t.original_reason_entitlement_code IS NULL;

-- Medicare status membership
SELECT 'elig_medicare_status_known' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*)     AS unknown_code_count
FROM (
  SELECT DISTINCT medicare_status_code AS code
  FROM eligibility
  WHERE medicare_status_code IS NOT NULL AND btrim(medicare_status_code) <> ''
) s
LEFT JOIN :"terminology_schema".medicare_status t
  ON s.code = t.medicare_status_code
WHERE t.medicare_status_code IS NULL;
