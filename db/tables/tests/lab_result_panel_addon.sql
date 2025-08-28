-- db/tests/lab_result_panel_addon.sql
-- Panel integrity checks for lab_result.
-- Expects psql var: :"schema"
SET search_path TO :"schema", public;

-- 1) Consistent normalized ORDER CODE per accession (+ data_source)
WITH grp AS (
  SELECT
    accession_number,
    data_source,
    COUNT(*) AS rows_in_group,
    COUNT(DISTINCT normalized_order_code) FILTER (WHERE normalized_order_code IS NOT NULL) AS distinct_norm_order_codes
  FROM lab_result
  WHERE accession_number IS NOT NULL AND data_source IS NOT NULL
  GROUP BY 1,2
)
SELECT 'lab_panel_norm_order_code_consistent' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS fail_group_count
FROM grp
WHERE rows_in_group > 1
  AND distinct_norm_order_codes > 1;

-- 2) Consistent normalized ORDER TYPE per accession (+ data_source)
WITH grp AS (
  SELECT
    accession_number,
    data_source,
    COUNT(*) AS rows_in_group,
    COUNT(DISTINCT normalized_order_type) FILTER (WHERE normalized_order_type IS NOT NULL) AS distinct_norm_order_types
  FROM lab_result
  WHERE accession_number IS NOT NULL AND data_source IS NOT NULL
  GROUP BY 1,2
)
SELECT 'lab_panel_norm_order_type_consistent' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS fail_group_count
FROM grp
WHERE rows_in_group > 1
  AND distinct_norm_order_types > 1;

-- 3) Single person per accession (+ data_source) [soft but highly recommended]
WITH grp AS (
  SELECT
    accession_number,
    data_source,
    COUNT(*) AS rows_in_group,
    COUNT(DISTINCT person_id) FILTER (WHERE person_id IS NOT NULL) AS distinct_persons
  FROM lab_result
  WHERE accession_number IS NOT NULL AND data_source IS NOT NULL
  GROUP BY 1,2
)
SELECT 'lab_panel_single_person_per_accession' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS fail_group_count
FROM grp
WHERE rows_in_group > 1
  AND distinct_persons > 1;

-- 4) Collection window span <= 24 hours per accession/person/data_source
--    (Only evaluate groups with >=2 non-null collection timestamps.)
WITH spans AS (
  SELECT
    accession_number,
    person_id,
    data_source,
    COUNT(*) FILTER (WHERE collection_datetime IS NOT NULL) AS col_cnt,
    MIN(collection_datetime) AS min_col_dt,
    MAX(collection_datetime) AS max_col_dt
  FROM lab_result
  WHERE accession_number IS NOT NULL
    AND data_source IS NOT NULL
  GROUP BY 1,2,3
)
SELECT 'lab_panel_collection_window_le_24h' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS fail_group_count
FROM spans
WHERE col_cnt >= 2
  AND max_col_dt IS NOT NULL
  AND min_col_dt IS NOT NULL
  AND (max_col_dt - min_col_dt) > INTERVAL '24 hours';

-- 5) Results should fall within 30 days of first collection per accession/person/data_source (soft)
WITH spans AS (
  SELECT
    accession_number,
    person_id,
    data_source,
    MIN(collection_datetime) AS min_col_dt,
    MAX(result_datetime)     AS max_res_dt
  FROM lab_result
  WHERE accession_number IS NOT NULL
    AND data_source IS NOT NULL
  GROUP BY 1,2,3
)
SELECT 'lab_panel_results_within_30d_of_collection' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS fail_group_count
FROM spans
WHERE min_col_dt IS NOT NULL
  AND max_res_dt IS NOT NULL
  AND (max_res_dt - min_col_dt) > INTERVAL '30 days';
