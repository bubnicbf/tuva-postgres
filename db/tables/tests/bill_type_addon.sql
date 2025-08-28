-- db/tests/bill_type_addon.sql
-- Soft checks for medical_claim.bill_type_code vs terminology.bill_type.
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- 1) Distinct bill_type_code values present in data but missing from terminology
WITH missing AS (
  SELECT DISTINCT m.bill_type_code AS code
  FROM medical_claim m
  LEFT JOIN :"terminology_schema".bill_type t
    ON m.bill_type_code = t.bill_type_code
  WHERE m.bill_type_code IS NOT NULL
    AND t.bill_type_code IS NULL
)
SELECT 'mc_bill_type_unknown_codes' AS test,
       (SELECT COUNT(*) FROM missing) = 0 AS pass,
       (SELECT COUNT(*) FROM missing)     AS unknown_code_count;

-- 2) Distinct bill_type_code values that are marked deprecated=1 in terminology
WITH deprecated_codes AS (
  SELECT DISTINCT m.bill_type_code AS code
  FROM medical_claim m
  JOIN :"terminology_schema".bill_type t
    ON m.bill_type_code = t.bill_type_code
  WHERE m.bill_type_code IS NOT NULL
    AND COALESCE(t.deprecated, 0) = 1
)
SELECT 'mc_bill_type_deprecated_codes' AS test,
       (SELECT COUNT(*) FROM deprecated_codes) = 0 AS pass,
       (SELECT COUNT(*) FROM deprecated_codes)     AS deprecated_code_count;

-- 3) Rows (not just codes) that use deprecated bill types (for scale visibility)
SELECT 'mc_bill_type_deprecated_rows' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*)     AS deprecated_row_count
FROM medical_claim m
JOIN :"terminology_schema".bill_type t
  ON m.bill_type_code = t.bill_type_code
WHERE m.bill_type_code IS NOT NULL
  AND COALESCE(t.deprecated, 0) = 1;
