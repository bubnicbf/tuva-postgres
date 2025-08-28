-- db/tests/hcpcs_code_addon.sql
-- Soft check: medical_claim.hcpcs_code must exist in terminology.hcpcs_code (ignoring seqnum/recid).
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- 1) Distinct unknown HCPCS codes in medical_claim
WITH term AS (
  SELECT DISTINCT UPPER(BTRIM(hcpcs)) AS hcpcs
  FROM :"terminology_schema".hcpcs_code
  WHERE hcpcs IS NOT NULL AND BTRIM(hcpcs) <> ''
),
missing AS (
  SELECT DISTINCT UPPER(BTRIM(m.hcpcs_code)) AS hcpcs
  FROM medical_claim m
  WHERE m.hcpcs_code IS NOT NULL AND BTRIM(m.hcpcs_code) <> ''
    AND NOT EXISTS (
      SELECT 1 FROM term t WHERE t.hcpcs = UPPER(BTRIM(m.hcpcs_code))
    )
)
SELECT 'mc_hcpcs_unknown_codes' AS test,
       (SELECT COUNT(*) FROM missing) = 0 AS pass,
       (SELECT COUNT(*) FROM missing)     AS unknown_code_count;

-- 2) Row-level occurrences using unknown HCPCS codes
WITH term AS (
  SELECT DISTINCT UPPER(BTRIM(hcpcs)) AS hcpcs
  FROM :"terminology_schema".hcpcs_code
  WHERE hcpcs IS NOT NULL AND BTRIM(hcpcs) <> ''
)
SELECT 'mc_hcpcs_unknown_rows' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*)     AS unknown_row_count
FROM medical_claim m
LEFT JOIN term t
  ON t.hcpcs = UPPER(BTRIM(m.hcpcs_code))
WHERE m.hcpcs_code IS NOT NULL
  AND BTRIM(m.hcpcs_code) <> ''
  AND t.hcpcs IS NULL;
