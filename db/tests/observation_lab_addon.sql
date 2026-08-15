-- db/tests/observation_lab_addon.sql
-- Lab-focused add-on checks for observations.
-- Expects psql vars: :"schema"
SET search_path TO :"schema", public;

-- Heuristics:
-- - A unit implies numeric if it matches common lab unit patterns (case-insensitive)
-- - We'll parse numbers from strings by stripping commas and picking the first numeric token
-- - Allowed non-numeric tokens when units imply numeric (to avoid false alarms):
--   POSITIVE/NEGATIVE/DETECTED/NOT DETECTED/REACTIVE/NONREACTIVE/TRACE/UNDETECTABLE

WITH base AS (
  SELECT
    observation_id,
    observation_type,
    result,
    source_units,
    normalized_units,
    normalized_reference_range_low  AS ref_low_raw,
    normalized_reference_range_high AS ref_high_raw
  FROM observation
),
units AS (
  SELECT
    b.*,
    -- Pick normalized_units first; fall back to source_units
    COALESCE(NULLIF(btrim(normalized_units), ''), NULLIF(btrim(source_units), '')) AS unit_pref,
    lower(COALESCE(NULLIF(btrim(normalized_units), ''), NULLIF(btrim(source_units), ''))) AS unit_pref_lc
  FROM base b
),
flags AS (
  SELECT
    u.*,
    -- Regex heuristic for numeric-like units
    CASE
      WHEN unit_pref_lc ~ '(mg|g|mcg|µg|ng|mmol|mEq|iu|u|iu\/|u\/|mm ?hg|10\\^|/u?l|/mm3|%|mol\/l|kat|mkat)'
      THEN true ELSE false
    END AS unit_implies_numeric
  FROM units u
),
parsed AS (
  SELECT
    f.*,
    -- Clean and extract numeric token from result (handles "<5", "5 %", "1,234.5", "1.2e3")
    NULLIF(btrim(replace(result, ',', '')), '') AS result_clean,
    substring(NULLIF(btrim(replace(result, ',', '')), '') from '[-+]?\d+(\.\d+)?([eE][-+]?\d+)?') AS result_num_str,

    -- Extract numeric from ref ranges if present (they may still include units in some feeds)
    substring(NULLIF(btrim(ref_low_raw),  '') from '[-+]?\d+(\.\d+)?([eE][-+]?\d+)?') AS ref_low_str,
    substring(NULLIF(btrim(ref_high_raw), '') from '[-+]?\d+(\.\d+)?([eE][-+]?\d+)?') AS ref_high_str
  FROM flags f
),
casted AS (
  SELECT
    p.*,
    CASE WHEN result_num_str  IS NOT NULL THEN result_num_str::numeric  END AS result_num,
    CASE WHEN ref_low_str     IS NOT NULL THEN ref_low_str::numeric     END AS ref_low_num,
    CASE WHEN ref_high_str    IS NOT NULL THEN ref_high_str::numeric    END AS ref_high_num
  FROM parsed p
),
norm AS (
  SELECT
    c.*,
    -- Allow common qualitative tokens even when units look numeric (prevents false alarms)
    CASE
      WHEN c.result_clean IS NULL THEN false
      WHEN lower(c.result_clean) IN ('pos','positive','neg','negative','detected','not detected','reactive','nonreactive','trace','undetectable') THEN true
      ELSE false
    END AS allow_qualitative
  FROM casted c
)

-- 1) If units imply numeric, result should be parseable OR be an allowed qualitative token
SELECT 'obs_lab_numeric_expected_but_not_numeric' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS fail_count
FROM norm
WHERE unit_implies_numeric
  AND result_clean IS NOT NULL
  AND result_num IS NULL
  AND NOT allow_qualitative;

-- 2) Reference range ordering: low <= high when both are numeric
SELECT 'obs_lab_ref_range_order' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS fail_count
FROM norm
WHERE ref_low_num IS NOT NULL
  AND ref_high_num IS NOT NULL
  AND ref_low_num > ref_high_num;

-- 3) Result below reference low (soft check; only when all numeric)
SELECT 'obs_lab_result_below_low' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS fail_count
FROM norm
WHERE result_num  IS NOT NULL
  AND ref_low_num IS NOT NULL
  AND result_num < ref_low_num;

-- 4) Result above reference high (soft check; only when all numeric)
SELECT 'obs_lab_result_above_high' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS fail_count
FROM norm
WHERE result_num   IS NOT NULL
  AND ref_high_num IS NOT NULL
  AND result_num > ref_high_num;
