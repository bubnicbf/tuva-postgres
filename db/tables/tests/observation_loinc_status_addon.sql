-- db/tests/observation_loinc_status_addon.sql
-- Soft surfacer: count usage of Deprecated/Discouraged LOINC codes in observation.
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- Normalize status text to handle case differences
WITH obs AS (
  SELECT o.normalized_code AS loinc
  FROM observation o
  WHERE o.normalized_code_type = 'LOINC'
    AND o.normalized_code IS NOT NULL
    AND btrim(o.normalized_code) <> ''
),
joined AS (
  SELECT o.loinc,
         COALESCE(t.status, '') AS status
  FROM obs o
  LEFT JOIN :"terminology_schema".loinc t
    ON o.loinc = t.loinc
),
status_norm AS (
  SELECT loinc,
         UPPER(btrim(status)) AS status_u
  FROM joined
  WHERE status IS NOT NULL AND btrim(status) <> ''
)

-- 1) DISTINCT deprecated codes used
SELECT 'obs_loinc_deprecated_codes' AS test,
       (SELECT COUNT(DISTINCT loinc) FROM status_norm WHERE status_u = 'DEPRECATED') = 0 AS pass,
       (SELECT COUNT(DISTINCT loinc) FROM status_norm WHERE status_u = 'DEPRECATED')     AS deprecated_code_count;

-- 2) ROWS using deprecated codes
SELECT 'obs_loinc_deprecated_rows' AS test,
       (SELECT COUNT(*) FROM status_norm WHERE status_u = 'DEPRECATED') = 0 AS pass,
       (SELECT COUNT(*) FROM status_norm WHERE status_u = 'DEPRECATED')     AS deprecated_row_count;

-- 3) DISTINCT discouraged codes used
SELECT 'obs_loinc_discouraged_codes' AS test,
       (SELECT COUNT(DISTINCT loinc) FROM status_norm WHERE status_u = 'DISCOURAGED') = 0 AS pass,
       (SELECT COUNT(DISTINCT loinc) FROM status_norm WHERE status_u = 'DISCOURAGED')     AS discouraged_code_count;

-- 4) ROWS using discouraged codes
SELECT 'obs_loinc_discouraged_rows' AS test,
       (SELECT COUNT(*) FROM status_norm WHERE status_u = 'DISCOURAGED') = 0 AS pass,
       (SELECT COUNT(*) FROM status_norm WHERE status_u = 'DISCOURAGED')     AS discouraged_row_count;
