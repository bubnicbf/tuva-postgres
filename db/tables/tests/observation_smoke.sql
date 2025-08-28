-- db/tests/observation_smoke.sql
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- 1) table has rows
SELECT 'observation_has_rows' AS test, COUNT(*) > 0 AS pass, COUNT(*) AS row_count
FROM observation;

-- 2) PK not null
SELECT 'observation_pk_not_null' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM observation
WHERE observation_id IS NULL;

-- 3) PK unique
SELECT 'observation_pk_unique' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS dup_key_count
FROM (
  SELECT observation_id FROM observation GROUP BY observation_id HAVING COUNT(*) > 1
) d;

-- 4) person_id must exist in patient (when referenced)
SELECT 'observation_person_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM observation o
LEFT JOIN patient p ON p.person_id = o.person_id
WHERE o.person_id IS NOT NULL AND p.person_id IS NULL;

-- 5) encounter_id must exist in encounter (when referenced)
SELECT 'observation_encounter_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM observation o
LEFT JOIN encounter e ON e.encounter_id = o.encounter_id
WHERE o.encounter_id IS NOT NULL AND e.encounter_id IS NULL;

-- 6) date logic: observation_date not in the future (when present)
SELECT 'observation_date_not_future' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM observation
WHERE observation_date IS NOT NULL
  AND observation_date > CURRENT_DATE;

-- 7) terminology: source_code_type must exist (when present)
SELECT 'observation_source_code_type_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_type_count
FROM observation o
LEFT JOIN :"terminology_schema".observation_code_type t
  ON o.source_code_type = t.code
WHERE o.source_code_type IS NOT NULL
  AND t.code IS NULL;

-- 8) person/encounter consistency: if both present, person_ids should match
SELECT 'observation_person_matches_encounter' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS mismatch_count
FROM observation o
JOIN encounter e ON e.encounter_id = o.encounter_id
WHERE o.encounter_id IS NOT NULL
  AND o.person_id   IS NOT NULL
  AND e.person_id   IS NOT NULL
  AND o.person_id <> e.person_id;

-- 9) (soft) duplicates on (person_id, panel_id, observation_date, data_source)
WITH dupes AS (
  SELECT
    person_id,
    panel_id,
    observation_date,
    data_source,
    COUNT(*) AS cnt
  FROM observation
  WHERE person_id IS NOT NULL
    AND panel_id  IS NOT NULL
    AND observation_date IS NOT NULL
    AND data_source IS NOT NULL
  GROUP BY 1,2,3,4
  HAVING COUNT(*) > 1
)
SELECT 'observation_soft_dupe_person_panel_date' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS dup_group_count
FROM dupes;

-- 10) tuva_last_run not in the future (when present)
SELECT 'observation_tuva_last_run_not_future' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM observation
WHERE tuva_last_run IS NOT NULL
  AND tuva_last_run > (NOW()::timestamp without time zone);
