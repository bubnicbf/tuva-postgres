-- db/tests/immunization_cvx_addon.sql
-- Soft check: immunization.normalized_code (when type='CVX') must exist in terminology.cvx.
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- Distinct unknown CVX codes
WITH missing AS (
  SELECT DISTINCT i.normalized_code AS cvx
  FROM immunization i
  LEFT JOIN :"terminology_schema".cvx t
    ON i.normalized_code = t.cvx
  WHERE i.normalized_code_type = 'CVX'
    AND i.normalized_code IS NOT NULL
    AND btrim(i.normalized_code) <> ''
    AND t.cvx IS NULL
)
SELECT 'imm_cvx_unknown_codes' AS test,
       (SELECT COUNT(*) FROM missing) = 0 AS pass,
       (SELECT COUNT(*) FROM missing)     AS unknown_code_count;

-- Row-level occurrences using unknown CVX codes (scale visibility)
SELECT 'imm_cvx_unknown_rows' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*)     AS unknown_row_count
FROM immunization i
LEFT JOIN :"terminology_schema".cvx t
  ON i.normalized_code = t.cvx
WHERE i.normalized_code_type = 'CVX'
  AND i.normalized_code IS NOT NULL
  AND btrim(i.normalized_code) <> ''
  AND t.cvx IS NULL;
