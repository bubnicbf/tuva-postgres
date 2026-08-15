-- db/tests/appointment_smoke.sql
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- 1) table has rows
SELECT 'appt_has_rows' AS test, COUNT(*) > 0 AS pass, COUNT(*) AS row_count
FROM appointment;

-- 2) PK not null
SELECT 'appt_pk_not_null' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM appointment
WHERE appointment_id IS NULL;

-- 3) PK unique
SELECT 'appt_pk_unique' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS dup_key_count
FROM (
  SELECT appointment_id FROM appointment GROUP BY appointment_id HAVING COUNT(*) > 1
) d;

-- 4) person_id must exist in patient (when referenced)
SELECT 'appt_person_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM appointment a
LEFT JOIN patient p ON p.person_id = a.person_id
WHERE a.person_id IS NOT NULL AND p.person_id IS NULL;

-- 5) encounter_id must exist in encounter (when referenced)
SELECT 'appt_encounter_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM appointment a
LEFT JOIN encounter e ON e.encounter_id = a.encounter_id
WHERE a.encounter_id IS NOT NULL AND e.encounter_id IS NULL;

-- 6) practitioner_id must exist in practitioner (when referenced)
SELECT 'appt_practitioner_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM appointment a
LEFT JOIN practitioner x ON x.practitioner_id = a.practitioner_id
WHERE a.practitioner_id IS NOT NULL AND x.practitioner_id IS NULL;

-- 7) date/duration logic: end >= start (when both present)
SELECT 'appt_end_ge_start' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM appointment
WHERE start_datetime IS NOT NULL
  AND end_datetime   IS NOT NULL
  AND end_datetime < start_datetime;

-- 8) duration non-negative (when present)
SELECT 'appt_duration_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM appointment
WHERE duration IS NOT NULL AND duration < 0;

-- 9) (soft) duration matches end-start within 1 minute when all present
WITH diffs AS (
  SELECT
    appointment_id,
    ROUND(EXTRACT(EPOCH FROM (end_datetime - start_datetime)) / 60.0, 2) AS mins_calc,
    duration
  FROM appointment
  WHERE start_datetime IS NOT NULL
    AND end_datetime   IS NOT NULL
    AND duration       IS NOT NULL
)
SELECT 'appt_duration_matches' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS fail_count
FROM diffs
WHERE ABS(duration - mins_calc) > 1.0;

-- 10) terminology: appointment type (source & normalized) must exist when present
SELECT 'appt_source_type_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_count
FROM appointment a
LEFT JOIN :"terminology_schema".appointment_type t
  ON a.source_appointment_type_code = t.code
WHERE a.source_appointment_type_code IS NOT NULL AND t.code IS NULL;

SELECT 'appt_normalized_type_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_count
FROM appointment a
LEFT JOIN :"terminology_schema".appointment_type t
  ON a.normalized_appointment_type_code = t.code
WHERE a.normalized_appointment_type_code IS NOT NULL AND t.code IS NULL;

-- 11) terminology: status (source & normalized) must exist when present
SELECT 'appt_source_status_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_count
FROM appointment a
LEFT JOIN :"terminology_schema".appointment_status s
  ON a.source_status = s.code
WHERE a.source_status IS NOT NULL AND s.code IS NULL;

SELECT 'appt_normalized_status_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_count
FROM appointment a
LEFT JOIN :"terminology_schema".appointment_status s
  ON a.normalized_status = s.code
WHERE a.normalized_status IS NOT NULL AND s.code IS NULL;

-- 12) terminology: reason & cancellation code-type presence (when present)
SELECT 'appt_source_reason_code_type_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_type_count
FROM appointment a
LEFT JOIN :"terminology_schema".appointment_reason_code_type rct
  ON a.source_reason_code_type = rct.code
WHERE a.source_reason_code_type IS NOT NULL AND rct.code IS NULL;

SELECT 'appt_normalized_reason_code_type_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_type_count
FROM appointment a
LEFT JOIN :"terminology_schema".appointment_reason_code_type rct
  ON a.normalized_reason_code_type = rct.code
WHERE a.normalized_reason_code_type IS NOT NULL AND rct.code IS NULL;

SELECT 'appt_source_cancel_code_type_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_type_count
FROM appointment a
LEFT JOIN :"terminology_schema".appointment_cancellation_reason_code_type crt
  ON a.source_cancellation_reason_code_type = crt.code
WHERE a.source_cancellation_reason_code_type IS NOT NULL AND crt.code IS NULL;

SELECT 'appt_normalized_cancel_code_type_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_type_count
FROM appointment a
LEFT JOIN :"terminology_schema".appointment_cancellation_reason_code_type crt
  ON a.normalized_cancellation_reason_code_type = crt.code
WHERE a.normalized_cancellation_reason_code_type IS NOT NULL AND crt.code IS NULL;

-- 13) person/encounter consistency: if both present, person_ids should match
SELECT 'appt_person_matches_encounter' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS mismatch_count
FROM appointment a
JOIN encounter e ON e.encounter_id = a.encounter_id
WHERE a.encounter_id IS NOT NULL
  AND a.person_id   IS NOT NULL
  AND e.person_id   IS NOT NULL
  AND a.person_id <> e.person_id;

-- 14) (soft) duplicates: (person_id, start_datetime, normalized_type_code, data_source)
WITH dupes AS (
  SELECT
    person_id,
    start_datetime,
    normalized_appointment_type_code,
    data_source,
    COUNT(*) AS cnt
  FROM appointment
  WHERE person_id IS NOT NULL
    AND start_datetime IS NOT NULL
    AND normalized_appointment_type_code IS NOT NULL
    AND data_source IS NOT NULL
  GROUP BY 1,2,3,4
  HAVING COUNT(*) > 1
)
SELECT 'appt_soft_dupe_person_start_normtype' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS dup_group_count
FROM dupes;

-- 15) tuva_last_run not in the future (when present)
SELECT 'appt_tuva_last_run_not_future' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM appointment
WHERE tuva_last_run IS NOT NULL
  AND tuva_last_run > (NOW()::timestamp without time zone);
