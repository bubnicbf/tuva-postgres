-- db/tests/observation_panel_addon.sql
-- Panel integrity checks for observation.panel_id.
-- Expects psql var: :"schema"
SET search_path TO :"schema", public;

-- 1) Consistent normalized_code per panel_id (+ data_source)
WITH grp AS (
  SELECT
    panel_id,
    data_source,
    COUNT(*) AS rows_in_group,
    COUNT(DISTINCT normalized_code) FILTER (WHERE normalized_code IS NOT NULL) AS distinct_norm_codes
  FROM observation
  WHERE panel_id IS NOT NULL AND data_source IS NOT NULL
  GROUP BY 1,2
)
SELECT 'obs_panel_norm_code_consistent' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS fail_group_count
FROM grp
WHERE rows_in_group > 1
  AND distinct_norm_codes > 1;

-- 2) Consistent normalized_code_type per panel_id (+ data_source)
WITH grp AS (
  SELECT
    panel_id,
    data_source,
    COUNT(*) AS rows_in_group,
    COUNT(DISTINCT normalized_code_type) FILTER (WHERE normalized_code_type IS NOT NULL) AS distinct_norm_code_types
  FROM observation
  WHERE panel_id IS NOT NULL AND data_source IS NOT NULL
  GROUP BY 1,2
)
SELECT 'obs_panel_norm_code_type_consistent' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS fail_group_count
FROM grp
WHERE rows_in_group > 1
  AND distinct_norm_code_types > 1;

-- 3) Single person per panel (+ data_source) [soft]
WITH grp AS (
  SELECT
    panel_id,
    data_source,
    COUNT(*) AS rows_in_group,
    COUNT(DISTINCT person_id) FILTER (WHERE person_id IS NOT NULL) AS distinct_persons
  FROM observation
  WHERE panel_id IS NOT NULL AND data_source IS NOT NULL
  GROUP BY 1,2
)
SELECT 'obs_panel_single_person_per_panel' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS fail_group_count
FROM grp
WHERE rows_in_group > 1
  AND distinct_persons > 1;

-- 4) Single encounter per panel (+ data_source) [soft]
WITH grp AS (
  SELECT
    panel_id,
    data_source,
    COUNT(*) AS rows_in_group,
    COUNT(DISTINCT encounter_id) FILTER (WHERE encounter_id IS NOT NULL) AS distinct_encounters
  FROM observation
  WHERE panel_id IS NOT NULL AND data_source IS NOT NULL
  GROUP BY 1,2
)
SELECT 'obs_panel_single_encounter_per_panel' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS fail_group_count
FROM grp
WHERE rows_in_group > 1
  AND distinct_encounters > 1;

-- 5) Observation date window span <= 1 day per panel/person/data_source (tight window)
--    Since observation_date is DATE (not timestamp), treat differences strictly by days.
WITH spans AS (
  SELECT
    panel_id,
    person_id,
    data_source,
    COUNT(*) FILTER (WHERE observation_date IS NOT NULL) AS dcnt,
    MIN(observation_date) AS min_dt,
    MAX(observation_date) AS max_dt
  FROM observation
  WHERE panel_id IS NOT NULL
    AND data_source IS NOT NULL
  GROUP BY 1,2,3
)
SELECT 'obs_panel_date_window_le_1d' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS fail_group_count
FROM spans
WHERE dcnt >= 2
  AND max_dt IS NOT NULL
  AND min_dt IS NOT NULL
  AND (max_dt - min_dt) > 1;

-- 6) (soft) Observations should fall within 30 days of the first date per panel/person/data_source
WITH spans AS (
  SELECT
    panel_id,
    person_id,
    data_source,
    MIN(observation_date) AS min_dt,
    MAX(observation_date) AS max_dt
  FROM observation
  WHERE panel_id IS NOT NULL
    AND data_source IS NOT NULL
  GROUP BY 1,2,3
)
SELECT 'obs_panel_dates_within_30d_of_first' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS fail_group_count
FROM spans
WHERE min_dt IS NOT NULL
  AND max_dt IS NOT NULL
  AND (max_dt - min_dt) > 30;
