-- db/tests/person_id_crosswalk_smoke.sql
-- Expects psql var: :"schema"
SET search_path TO :"schema", public;

-- 1) table has rows
SELECT 'pxw_has_rows' AS test, COUNT(*) > 0 AS pass, COUNT(*) AS row_count
FROM person_id_crosswalk;

-- 2) person_id not null
SELECT 'pxw_person_id_not_null' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM person_id_crosswalk
WHERE person_id IS NULL;

-- 3) person_id exists in patient (FK presence; helpful even if FK is deferrable)
SELECT 'pxw_person_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM person_id_crosswalk x
LEFT JOIN patient p ON p.person_id = x.person_id
WHERE x.person_id IS NOT NULL AND p.person_id IS NULL;

-- 4) require at least one meaningful handle (patient_id or member_id non-blank)
SELECT 'pxw_handle_present_nonblank' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM person_id_crosswalk
WHERE COALESCE(NULLIF(BTRIM(patient_id), ''), NULL) IS NULL
  AND COALESCE(NULLIF(BTRIM(member_id), ''), NULL) IS NULL;

-- 5) null-safe uniqueness over (person_id, patient_id, member_id, payer, plan, data_source)
--    (mirrors the expression UNIQUE index definition with COALESCE(''))
WITH dupes AS (
  SELECT
    person_id,
    COALESCE(patient_id, '')   AS patient_id_n,
    COALESCE(member_id, '')    AS member_id_n,
    COALESCE(payer, '')        AS payer_n,
    COALESCE(plan, '')         AS plan_n,
    COALESCE(data_source, '')  AS data_source_n,
    COUNT(*) AS cnt
  FROM person_id_crosswalk
  GROUP BY 1,2,3,4,5,6
  HAVING COUNT(*) > 1
)
SELECT 'pxw_unique_tuple' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS dup_group_count
FROM dupes;

-- 6) (soft) patient_id+data_source should map to a single person_id (when patient_id provided)
WITH bad_map AS (
  SELECT patient_id, data_source, COUNT(DISTINCT person_id) AS person_count
  FROM person_id_crosswalk
  WHERE COALESCE(NULLIF(BTRIM(patient_id), ''), NULL) IS NOT NULL
  GROUP BY 1,2
  HAVING COUNT(DISTINCT person_id) > 1
)
SELECT 'pxw_patient_map_unique' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_group_count
FROM bad_map;

-- 7) (soft) member_id+payer+plan should map to a single person_id (when member_id provided)
WITH bad_member_map AS (
  SELECT member_id, payer, plan, COUNT(DISTINCT person_id) AS person_count
  FROM person_id_crosswalk
  WHERE COALESCE(NULLIF(BTRIM(member_id), ''), NULL) IS NOT NULL
  GROUP BY 1,2,3
  HAVING COUNT(DISTINCT person_id) > 1
)
SELECT 'pxw_member_map_unique' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_group_count
FROM bad_member_map;

-- 8) ensure the null-safe UNIQUE index exists (defense against accidental drops/renames)
SELECT 'pxw_unique_index_exists' AS test, EXISTS (
  SELECT 1
  FROM pg_indexes
  WHERE schemaname = current_schema()
    AND indexname = 'pxw_unique_key'
) AS pass, 0 AS info;
