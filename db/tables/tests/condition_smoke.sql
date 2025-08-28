-- db/tests/condition_smoke.sql
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- 1) table has rows
SELECT 'condition_has_rows' AS test, COUNT(*) > 0 AS pass, COUNT(*) AS row_count
FROM condition;

-- 2) PK not null
SELECT 'condition_pk_not_null' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM condition
WHERE condition_id IS NULL;

-- 3) PK unique
SELECT 'condition_pk_unique' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS dup_key_count
FROM (
  SELECT condition_id FROM condition GROUP BY condition_id HAVING COUNT(*) > 1
) d;

-- 4) person_id must exist in patient (when referenced)
SELECT 'condition_person_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM condition c
LEFT JOIN patient p ON p.person_id = c.person_id
WHERE c.person_id IS NOT NULL AND p.person_id IS NULL;

-- 5) encounter_id must exist in encounter (when referenced)
SELECT 'condition_encounter_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM condition c
LEFT JOIN encounter e ON e.encounter_id = c.encounter_id
WHERE c.encounter_id IS NOT NULL AND e.encounter_id IS NULL;

-- 6) date logic: resolved_date >= onset_date (when both present)
SELECT 'condition_resolved_ge_onset' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM condition
WHERE onset_date IS NOT NULL
  AND resolved_date IS NOT NULL
  AND resolved_date < onset_date;

-- 7) date logic: recorded_date >= onset_date (when both present)
SELECT 'condition_recorded_ge_onset' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM condition
WHERE onset_date IS NOT NULL
  AND recorded_date IS NOT NULL
  AND recorded_date < onset_date;

-- 8) (soft) recorded_date <= resolved_date (when both present)
SELECT 'condition_recorded_le_resolved' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM condition
WHERE recorded_date IS NOT NULL
  AND resolved_date IS NOT NULL
  AND recorded_date > resolved_date;

-- 9) terminology: source_code_type must exist (when present)
SELECT 'condition_source_code_type_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_type_count
FROM condition c
LEFT JOIN :"terminology_schema".code_type t
  ON c.source_code_type = t.code_type
WHERE c.source_code_type IS NOT NULL
  AND t.code IS NULL;

-- 10) terminology: normalized_code_type must exist (when present)
SELECT 'condition_normalized_code_type_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_type_count
FROM condition c
LEFT JOIN :"terminology_schema".code_type t
  ON c.normalized_code_type = t.code_type
WHERE c.normalized_code_type IS NOT NULL
  AND t.code IS NULL;

-- 11) terminology: present_on_admit_code must exist (when present)
SELECT 'condition_poa_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_count
FROM condition c
LEFT JOIN :"terminology_schema".present_on_admit poa
  ON c.present_on_admit_code = poa.code
WHERE c.present_on_admit_code IS NOT NULL
  AND poa.code IS NULL;

-- 12) person/encounter consistency: if both present, person_ids should match
SELECT 'condition_person_matches_encounter' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS mismatch_count
FROM condition c
JOIN encounter e ON e.encounter_id = c.encounter_id
WHERE c.encounter_id IS NOT NULL
  AND c.person_id   IS NOT NULL
  AND e.person_id   IS NOT NULL
  AND c.person_id <> e.person_id;

-- 13) condition_rank positive (when present)
SELECT 'condition_rank_positive' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM condition
WHERE condition_rank IS NOT NULL
  AND condition_rank < 1;

-- 14) (soft) duplicates on (person_id, COALESCE(onset_date, recorded_date), normalized_code, data_source)
WITH keyed AS (
  SELECT
    person_id,
    COALESCE(onset_date, recorded_date) AS dx_date,
    normalized_code,
    data_source
  FROM condition
  WHERE person_id IS NOT NULL
    AND normalized_code IS NOT NULL
    AND data_source IS NOT NULL
    AND (onset_date IS NOT NULL OR recorded_date IS NOT NULL)
),
dupes AS (
  SELECT person_id, dx_date, normalized_code, data_source, COUNT(*) AS cnt
  FROM keyed
  GROUP BY 1,2,3,4
  HAVING COUNT(*) > 1
)
SELECT 'condition_soft_dupe_person_date_normcode' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS dup_group_count
FROM dupes;

-- 15) tuva_last_run not in the future (when present)
SELECT 'condition_tuva_last_run_not_future' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM condition
WHERE tuva_last_run IS NOT NULL
  AND tuva_last_run > (NOW()::timestamp without time zone);
