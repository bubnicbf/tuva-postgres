-- db/tests/ndc_addon.sql
-- Soft checks for NDC membership and RXCUI presence.
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

WITH pc AS (
  -- Normalize pharmacy NDCs to digits-only
  SELECT
    pharmacy_claim_id,
    REGEXP_REPLACE(ndc_code, '\D', '', 'g') AS ndc_digits
  FROM pharmacy_claim
  WHERE ndc_code IS NOT NULL AND btrim(ndc_code) <> ''
),
term AS (
  -- Normalize terminology NDCs to digits-only + carry RXCUI
  SELECT
    REGEXP_REPLACE(ndc, '\D', '', 'g') AS ndc_digits,
    rxcui
  FROM :"terminology_schema".ndc
  WHERE ndc IS NOT NULL AND btrim(ndc) <> ''
),

-- (Optional noise filter): only consider plausibly formed NDCs (10 or 11 digits)
pc_plausible AS (
  SELECT * FROM pc WHERE length(ndc_digits) IN (10,11)
)

-- 1) Distinct NDCs in pharmacy_claim not found in terminology.ndc
, missing_codes AS (
  SELECT DISTINCT p.ndc_digits
  FROM pc_plausible p
  LEFT JOIN term t ON p.ndc_digits = t.ndc_digits
  WHERE t.ndc_digits IS NULL
)

SELECT 'pharmacy_ndc_unknown_codes' AS test,
       (SELECT COUNT(*) FROM missing_codes) = 0 AS pass,
       (SELECT COUNT(*) FROM missing_codes)     AS unknown_code_count;

-- 2) Row-level occurrences using unknown NDCs
SELECT 'pharmacy_ndc_unknown_rows' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*)     AS unknown_row_count
FROM pc_plausible p
LEFT JOIN term t ON p.ndc_digits = t.ndc_digits
WHERE t.ndc_digits IS NULL;

-- 3) Rows where NDC is known but RXCUI is missing/blank (scale visibility)
SELECT 'pharmacy_ndc_missing_rxcui_rows' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*)     AS missing_rxcui_row_count
FROM pc_plausible p
JOIN term t ON p.ndc_digits = t.ndc_digits
WHERE t.rxcui IS NULL OR btrim(t.rxcui) = '';

-- (Optional) Distinct NDCs with missing RXCUI (quick inventory)
WITH ndc_missing AS (
  SELECT DISTINCT p.ndc_digits
  FROM pc_plausible p
  JOIN term t ON p.ndc_digits = t.ndc_digits
  WHERE t.rxcui IS NULL OR btrim(t.rxcui) = ''
)
SELECT 'pharmacy_ndc_missing_rxcui_codes' AS test,
       (SELECT COUNT(*) FROM ndc_missing) = 0 AS pass,
       (SELECT COUNT(*) FROM ndc_missing)     AS missing_rxcui_code_count;
