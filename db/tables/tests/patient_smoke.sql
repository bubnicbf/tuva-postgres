-- db/tests/patient_smoke.sql
-- Expects psql vars: :"schema"
SET search_path TO :"schema", public;

-- 1) table has rows
SELECT 'patient_has_rows' AS test, COUNT(*) > 0 AS pass, COUNT(*) AS row_count
FROM patient;

-- 2) PK not null
SELECT 'patient_person_id_not_null' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM patient WHERE person_id IS NULL;

-- 3) PK unique
SELECT 'patient_person_id_unique' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS dup_key_count
FROM (
  SELECT person_id FROM patient GROUP BY person_id HAVING COUNT(*) > 1
) d;

-- 4) birth_date not in future
SELECT 'patient_birth_not_future' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM patient
WHERE birth_date IS NOT NULL AND birth_date > CURRENT_DATE;

-- 5) death after birth (when both present)
SELECT 'patient_death_after_birth' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM patient
WHERE death_date IS NOT NULL AND birth_date IS NOT NULL AND death_date < birth_date;

-- 6) death_flag must be 0/1 (or NULL)
SELECT 'patient_death_flag_boolish' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM patient
WHERE death_flag IS NOT NULL AND death_flag NOT IN (0,1);

-- 7) latitude/longitude in range (when present)
SELECT 'patient_lat_range' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM patient
WHERE latitude IS NOT NULL AND NOT (latitude BETWEEN -90 AND 90);

SELECT 'patient_lon_range' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM patient
WHERE longitude IS NOT NULL AND NOT (longitude BETWEEN -180 AND 180);

-- 8) email looks like an email (very light)
SELECT 'patient_email_format' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM patient
WHERE email IS NOT NULL AND POSITION('@' IN email) = 0;

-- 9) SSN: exactly 9 digits after stripping non-digits (when present)
SELECT 'patient_ssn_9_digits' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM patient
WHERE social_security_number IS NOT NULL
  AND LENGTH(REGEXP_REPLACE(social_security_number, '\D', '', 'g')) <> 9;

-- 10) age consistent with birth_date & tuva_last_run (when all present)
--     age() returns an interval; extract year gives floor (years) difference.
SELECT 'patient_age_consistent' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM patient
WHERE birth_date IS NOT NULL
  AND tuva_last_run IS NOT NULL
  AND age IS NOT NULL
  AND age <> GREATEST(0, EXTRACT(YEAR FROM age(tuva_last_run::timestamp, birth_date::timestamp))::int);

-- 11) age_group matches decade bucket (when age present)
--     Expected: '0-9', '10-19', ..., '90-99', '100+'
SELECT 'patient_age_group_consistent' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM patient
WHERE age IS NOT NULL
  AND age_group IS NOT NULL
  AND (
    (age >= 100 AND age_group <> '100+')
    OR
    (age BETWEEN 0 AND 99 AND age_group <> ((age/10)*10)::text || '-' || (((age/10)*10)+9)::text)
  );
