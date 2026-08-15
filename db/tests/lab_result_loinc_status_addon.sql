-- db/tests/lab_result_loinc_status_addon.sql
-- Soft surfacer: Deprecated/Discouraged LOINC usage in lab_result
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

WITH loinc_refs AS (
  -- Collect LOINC codes from either normalized_order_* or normalized_component_*
  SELECT lr.lab_result_id, lr.normalized_order_code      AS loinc
  FROM lab_result lr
  WHERE lr.normalized_order_type = 'LOINC'
    AND lr.normalized_order_code IS NOT NULL
    AND btrim(lr.normalized_order_code) <> ''

  UNION ALL

  SELECT lr.lab_result_id, lr.normalized_component_code  AS loinc
  FROM lab_result lr
  WHERE lr.normalized_component_type = 'LOINC'
    AND lr.normalized_component_code IS NOT NULL
    AND btrim(lr.normalized_component_code) <> ''
),
joined AS (
  SELECT r.lab_result_id,
         r.loinc,
         COALESCE(t.status, '') AS status
  FROM loinc_refs r
  LEFT JOIN :"terminology_schema".loinc t
    ON r.loinc = t.loinc
),
status_norm AS (
  SELECT lab_result_id,
         loinc,
         UPPER(btrim(status)) AS status_u
  FROM joined
  WHERE status IS NOT NULL AND btrim(status) <> ''
)

-- 1) DISTINCT deprecated codes used
SELECT 'lr_loinc_deprecated_codes' AS test,
       (SELECT COUNT(DISTINCT loinc) FROM status_norm WHERE status_u = 'DEPRECATED') = 0 AS pass,
       (SELECT COUNT(DISTINCT loinc) FROM status_norm WHERE status_u = 'DEPRECATED')     AS deprecated_code_count;

-- 2) DISTINCT lab_result rows using any deprecated code
SELECT 'lr_loinc_deprecated_rows' AS test,
       (SELECT COUNT(DISTINCT lab_result_id) FROM status_norm WHERE status_u = 'DEPRECATED') = 0 AS pass,
       (SELECT COUNT(DISTINCT lab_result_id) FROM status_norm WHERE status_u = 'DEPRECATED')     AS deprecated_row_count;

-- 3) DISTINCT discouraged codes used
SELECT 'lr_loinc_discouraged_codes' AS test,
       (SELECT COUNT(DISTINCT loinc) FROM status_norm WHERE status_u = 'DISCOURAGED') = 0 AS pass,
       (SELECT COUNT(DISTINCT loinc) FROM status_norm WHERE status_u = 'DISCOURAGED')     AS discouraged_code_count;

-- 4) DISTINCT lab_result rows using any discouraged code
SELECT 'lr_loinc_discouraged_rows' AS test,
       (SELECT COUNT(DISTINCT lab_result_id) FROM status_norm WHERE status_u = 'DISCOURAGED') = 0 AS pass,
       (SELECT COUNT(DISTINCT lab_result_id) FROM status_norm WHERE status_u = 'DISCOURAGED')     AS discouraged_row_count;
