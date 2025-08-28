-- db/tests/lab_result_smoke.sql
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- 1) table has rows
SELECT 'lab_result_has_rows' AS test, COUNT(*) > 0 AS pass, COUNT(*) AS row_count
FROM lab_result;

-- 2) PK not null
SELECT 'lab_result_pk_not_null' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM lab_result
WHERE lab_result_id IS NULL;

-- 3) PK unique
SELECT 'lab_result_pk_unique' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS dup_key_count
FROM (
  SELECT lab_result_id FROM lab_result GROUP BY lab_result_id HAVING COUNT(*) > 1
) d;

-- 4) person_id must exist in patient (when referenced)
SELECT 'lab_result_person_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM lab_result lr
LEFT JOIN patient p ON p.person_id = lr.person_id
WHERE lr.person_id IS NOT NULL AND p.person_id IS NULL;

-- 5) encounter_id must exist in encounter (when referenced)
SELECT 'lab_result_encounter_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM lab_result lr
LEFT JOIN encounter e ON e.encounter_id = lr.encounter_id
WHERE lr.encounter_id IS NOT NULL AND e.encounter_id IS NULL;

-- 6) date ordering: result_datetime >= collection_datetime (when both present)
SELECT 'lab_result_dates_order' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM lab_result
WHERE collection_datetime IS NOT NULL
  AND result_datetime     IS NOT NULL
  AND result_datetime < collection_datetime;

-- 7) optional: timestamps not in the future (when present)
SELECT 'lab_result_result_dt_not_future' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM lab_result
WHERE result_datetime IS NOT NULL
  AND result_datetime > (NOW()::timestamp without time zone);

SELECT 'lab_result_collect_dt_not_future' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM lab_result
WHERE collection_datetime IS NOT NULL
  AND collection_datetime > (NOW()::timestamp without time zone);

-- 8) terminology: order code-types must exist (when present)
SELECT 'lab_result_source_order_type_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_type_count
FROM lab_result lr
LEFT JOIN :"terminology_schema".lab_order_code_type t
  ON lr.source_order_type = t.code
WHERE lr.source_order_type IS NOT NULL
  AND t.code IS NULL;

SELECT 'lab_result_normalized_order_type_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_type_count
FROM lab_result lr
LEFT JOIN :"terminology_schema".lab_order_code_type t
  ON lr.normalized_order_type = t.code
WHERE lr.normalized_order_type IS NOT NULL
  AND t.code IS NULL;

-- 9) person/encounter consistency: if both present, person_ids should match
SELECT 'lab_result_person_matches_encounter' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS mismatch_count
FROM lab_result lr
JOIN encounter e ON e.encounter_id = lr.encounter_id
WHERE lr.encounter_id IS NOT NULL
  AND lr.person_id    IS NOT NULL
  AND e.person_id     IS NOT NULL
  AND lr.person_id <> e.person_id;

-- 10) Numeric plausibility & reference ranges (normalized preferred, fallback to source)
WITH base AS (
  SELECT
    lab_result_id,
    result,
    -- prefer normalized units; fallback to source
    COALESCE(NULLIF(btrim(normalized_units), ''), NULLIF(btrim(source_units), '')) AS unit_pref,
    normalized_reference_range_low  AS n_low_raw,
    normalized_reference_range_high AS n_high_raw,
    source_reference_range_low      AS s_low_raw,
    source_reference_range_high     AS s_high_raw
  FROM lab_result
),
units AS (
  SELECT
    lab_result_id,
    result,
    unit_pref,
    lower(unit_pref) AS unit_pref_lc,
    COALESCE(NULLIF(btrim(n_low_raw),  ''), NULLIF(btrim(s_low_raw),  '')) AS ref_low_raw,
    COALESCE(NULLIF(btrim(n_high_raw), ''), NULLIF(btrim(s_high_raw), '')) AS ref_high_raw
  FROM base
),
flags AS (
  SELECT
    u.*,
    CASE
      WHEN unit_pref_lc ~ '(mg|g|mcg|µg|ng|mmol|mEq|iu|u|iu\/|u\/|mm ?hg|10\\^|/u?l|/mm3|%|mol\/l|kat|mkat)'
      THEN true ELSE false
    END AS unit_implies_numeric
  FROM units u
),
parsed AS (
  SELECT
    f.*,
    NULLIF(btrim(replace(result, ',', '')), '') AS result_clean,
    substring(NULLIF(btrim(replace(result, ',', '')), '') from '[-+]?\d+(\.\d+)?([eE][-+]?\d+)?') AS result_num_str,
    substring(ref_low_raw  from '[-+]?\d+(\.\d+)?([eE][-+]?\d+)?') AS ref_low_str,
    substring(ref_high_raw from '[-+]?\d+(\.\d+)?([eE][-+]?\d+)?') AS ref_high_str
  FROM flags f
),
casted AS (
  SELECT
    p.*,
    CASE WHEN result_num_str IS NOT NULL THEN result_num_str::numeric END AS result_num,
    CASE WHEN ref_low_str    IS NOT NULL THEN ref_low_str::numeric    END AS ref_low_num,
    CASE WHEN ref_high_str   IS NOT NULL THEN ref_high_str::numeric   END AS ref_high_num
  FROM parsed p
),
norm AS (
  SELECT
    c.*,
    CASE
      WHEN c.result_clean IS NULL THEN false
      WHEN lower(c.result_clean) IN ('pos','positive','neg','negative','detected','not detected','reactive','nonreactive','trace','undetectable')
        THEN true
      ELSE false
    END AS allow_qualitative
  FROM casted c
)

-- 10a) If units imply numeric, result should be parseable OR be allowed qualitative token
SELECT 'lab_result_numeric_expected_but_not_numeric' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS fail_count
FROM norm
WHERE unit_implies_numeric
  AND result_clean IS NOT NULL
  AND result_num IS NULL
  AND NOT allow_qualitative;

-- 10b) Reference range ordering (when both numeric)
SELECT 'lab_result_ref_range_order' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS fail_count
FROM norm
WHERE ref_low_num IS NOT NULL
  AND ref_high_num IS NOT NULL
  AND ref_low_num > ref_high_num;

-- 10c) Result below reference low (soft)
SELECT 'lab_result_result_below_low' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS fail_count
FROM norm
WHERE result_num  IS NOT NULL
  AND ref_low_num IS NOT NULL
  AND result_num < ref_low_num;

-- 10d) Result above reference high (soft)
SELECT 'lab_result_result_above_high' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS fail_count
FROM norm
WHERE result_num   IS NOT NULL
  AND ref_high_num IS NOT NULL
  AND result_num > ref_high_num;

-- 11) (soft) duplicates by accession + component (prefer normalized component; fallback to source)
WITH keyed AS (
  SELECT
    lab_result_id,
    accession_number,
    COALESCE(NULLIF(btrim(normalized_component_code), ''), NULLIF(btrim(source_component_code), '')) AS component_key,
    person_id,
    data_source
  FROM lab_result
),
dupes AS (
  SELECT accession_number, component_key, person_id, data_source, COUNT(*) AS cnt
  FROM keyed
  WHERE accession_number IS NOT NULL
    AND component_key   IS NOT NULL
    AND data_source     IS NOT NULL
  GROUP BY 1,2,3,4
  HAVING COUNT(*) > 1
)
SELECT 'lab_result_soft_dupe_accession_component' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS dup_group_count
FROM dupes;

-- 12) tuva_last_run not in the future (when present)
SELECT 'lab_result_tuva_last_run_not_future' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM lab_result
WHERE tuva_last_run IS NOT NULL
  AND tuva_last_run > (NOW()::timestamp without time zone);
