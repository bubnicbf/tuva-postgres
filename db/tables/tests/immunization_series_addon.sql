-- db/tests/immunization_series_addon.sql
-- Add-on: per-person CVX vaccine series logic (min spacing & monotonic dose numbers).
-- Expects psql var: :"schema"
SET search_path TO :"schema", public;

-- === Configuration ==========================================================
-- Default minimum spacing between consecutive doses (days).
-- Override per CVX by adding rows in the rules CTE (keep it small & curated).
WITH rules AS (
  -- cvx_code, min_days_between_consecutive_doses
  SELECT NULL::varchar AS cvx_code, 21::int AS min_days  -- default for all CVX
  UNION ALL SELECT '208', 21  -- Pfizer-BioNTech (prime series)
  UNION ALL SELECT '207', 28  -- Moderna (prime series)
  -- Add more overrides as needed...
),

-- === Dose parsing & sequencing =============================================
base AS (
  SELECT
    immunization_id,
    person_id,
    data_source,
    occurrence_date,
    normalized_code_type,
    normalized_code,             -- CVX expected here
    COALESCE(NULLIF(btrim(normalized_dose), ''), NULLIF(btrim(source_dose), '')) AS dose_text
  FROM immunization
  WHERE normalized_code_type = 'CVX'
    AND normalized_code IS NOT NULL
    AND occurrence_date IS NOT NULL
),
parsed AS (
  SELECT
    b.*,
    lower(b.dose_text) AS dose_text_lc,
    -- Pull first integer found (e.g., "dose 2", "#3", "3rd", "booster 3") as a fallback.
    substring(b.dose_text from '([0-9]{1,2})') AS dose_num_str
  FROM base b
),
dose_num AS (
  SELECT
    p.*,
    -- Heuristic mapping of common words -> numbers, else fallback to first digits found.
    COALESCE(
      CASE
        WHEN dose_text_lc ~ '\b(first|1st)\b'   THEN 1
        WHEN dose_text_lc ~ '\b(second|2nd)\b'  THEN 2
        WHEN dose_text_lc ~ '\b(third|3rd)\b'   THEN 3
        WHEN dose_text_lc ~ '\b(fourth|4th)\b'  THEN 4
        WHEN dose_text_lc ~ '\b(fifth|5th)\b'   THEN 5
        WHEN dose_text_lc ~ '\b(booster)\b'     THEN NULL   -- boosters vary; ignore for spacing unless numbered
        ELSE NULL
      END,
      NULLIF(dose_num_str,'')::int
    ) AS dose_number
  FROM parsed p
),
sequenced AS (
  SELECT
    d.*,
    LAG(occurrence_date) OVER (PARTITION BY person_id, normalized_code, data_source
                               ORDER BY occurrence_date, immunization_id) AS prev_date,
    LAG(dose_number)     OVER (PARTITION BY person_id, normalized_code, data_source
                               ORDER BY occurrence_date, immunization_id) AS prev_dose
  FROM dose_num d
),
applied_rules AS (
  SELECT
    s.*,
    COALESCE(r2.min_days, r1.min_days) AS min_days_required
  FROM sequenced s
  -- Specific CVX override (exact match), else fall back to default NULL row
  LEFT JOIN rules r2 ON r2.cvx_code = s.normalized_code
  LEFT JOIN rules r1 ON r1.cvx_code IS NULL
)

-- === Tests =================================================================
-- 1) Min spacing between consecutive doses: if dose_number increases, ensure gap >= min_days
SELECT 'imm_series_min_spacing' AS test,
       COUNT(*) = 0             AS pass,
       COUNT(*)                 AS fail_count
FROM applied_rules
WHERE dose_number IS NOT NULL
  AND prev_dose IS NOT NULL
  AND dose_number > prev_dose                -- only check advancing doses
  AND prev_date IS NOT NULL
  AND (occurrence_date - prev_date) < (min_days_required || ' days')::interval;

-- 2) Monotonic dose numbering over time (soft): later date should not have a smaller dose number
SELECT 'imm_series_monotonic_dose_numbers' AS test,
       COUNT(*) = 0                         AS pass,
       COUNT(*)                             AS fail_count
FROM applied_rules
WHERE dose_number IS NOT NULL
  AND prev_dose IS NOT NULL
  AND prev_date IS NOT NULL
  AND occurrence_date >= prev_date          -- only consider non-decreasing dates
  AND dose_number < prev_dose;
