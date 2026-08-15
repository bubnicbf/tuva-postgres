-- db/tests/immunization_smoke.sql
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- 1) table has rows
SELECT 'imm_has_rows' AS test, COUNT(*) > 0 AS pass, COUNT(*) AS row_count
FROM immunization;

-- 2) PK not null
SELECT 'imm_pk_not_null' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM immunization
WHERE immunization_id IS NULL;

-- 3) PK unique
SELECT 'imm_pk_unique' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS dup_key_count
FROM (
  SELECT immunization_id FROM immunization GROUP BY immunization_id HAVING COUNT(*) > 1
) d;

-- 4) person_id must exist in patient (when referenced)
SELECT 'imm_person_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM immunization i
LEFT JOIN patient p ON p.person_id = i.person_id
WHERE i.person_id IS NOT NULL AND p.person_id IS NULL;

-- 5) encounter_id must exist in encounter (when referenced)
SELECT 'imm_encounter_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM immunization i
LEFT JOIN encounter e ON e.encounter_id = i.encounter_id
WHERE i.encounter_id IS NOT NULL AND e.encounter_id IS NULL;

-- 6) practitioner_id must exist in practitioner (when referenced)
SELECT 'imm_practitioner_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM immunization i
LEFT JOIN practitioner x ON x.practitioner_id = i.practitioner_id
WHERE i.practitioner_id IS NOT NULL AND x.practitioner_id IS NULL;

-- 7) date sanity: occurrence_date not in the future (when present)
SELECT 'imm_occurrence_not_future' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM immunization
WHERE occurrence_date IS NOT NULL
  AND occurrence_date > CURRENT_DATE;

-- 8) ingest_datetime not in the future (when present)
SELECT 'imm_ingest_not_future' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM immunization
WHERE ingest_datetime IS NOT NULL
  AND ingest_datetime > (NOW()::timestamp without time zone);

-- 9) (soft) ingest after or same day as occurrence (when both present)
SELECT 'imm_ingest_on_or_after_occurrence' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM immunization
WHERE occurrence_date  IS NOT NULL
  AND ingest_datetime  IS NOT NULL
  AND (ingest_datetime::date) < occurrence_date;

-- 10) terminology: code-type presence (when present)
SELECT 'imm_source_code_type_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_type_count
FROM immunization i
LEFT JOIN :"terminology_schema".immunization_code_type t
  ON i.source_code_type = t.code
WHERE i.source_code_type IS NOT NULL
  AND t.code IS NULL;

SELECT 'imm_normalized_code_type_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_type_count
FROM immunization i
LEFT JOIN :"terminology_schema".immunization_code_type t
  ON i.normalized_code_type = t.code
WHERE i.normalized_code_type IS NOT NULL
  AND t.code IS NULL;

-- 11) CVX plausibility (digits-only) when type is CVX
SELECT 'imm_cvx_norm_digits_only' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM immunization
WHERE normalized_code_type = 'CVX'
  AND normalized_code IS NOT NULL
  AND BTRIM(normalized_code) <> ''
  AND normalized_code !~ '^[0-9]+$';

SELECT 'imm_cvx_source_digits_only' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM immunization
WHERE source_code_type = 'CVX'
  AND source_code IS NOT NULL
  AND BTRIM(source_code) <> ''
  AND source_code !~ '^[0-9]+$';

-- 12) person/encounter consistency: if both present, person_ids should match
SELECT 'imm_person_matches_encounter' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS mismatch_count
FROM immunization i
JOIN encounter e ON e.encounter_id = i.encounter_id
WHERE i.encounter_id IS NOT NULL
  AND i.person_id   IS NOT NULL
  AND e.person_id   IS NOT NULL
  AND i.person_id <> e.person_id;

-- 13) (soft) duplicates on (person_id, occurrence_date, normalized_code, data_source)
WITH dupes AS (
  SELECT
    person_id,
    occurrence_date,
    normalized_code,
    data_source,
    COUNT(*) AS cnt
  FROM immunization
  WHERE person_id        IS NOT NULL
    AND occurrence_date  IS NOT NULL
    AND normalized_code  IS NOT NULL
    AND data_source      IS NOT NULL
  GROUP BY 1,2,3,4
  HAVING COUNT(*) > 1
)
SELECT 'imm_soft_dupe_person_date_normcode' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS dup_group_count
FROM dupes;
