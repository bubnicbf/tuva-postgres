-- db/tests/medication_smoke.sql
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- 1) table has rows
SELECT 'med_has_rows' AS test, COUNT(*) > 0 AS pass, COUNT(*) AS row_count
FROM medication;

-- 2) PK not null
SELECT 'med_pk_not_null' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medication
WHERE medication_id IS NULL;

-- 3) PK unique
SELECT 'med_pk_unique' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS dup_key_count
FROM (
  SELECT medication_id FROM medication GROUP BY medication_id HAVING COUNT(*) > 1
) d;

-- 4) person_id must exist in patient (when referenced)
SELECT 'med_person_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM medication m
LEFT JOIN patient p ON p.person_id = m.person_id
WHERE m.person_id IS NOT NULL AND p.person_id IS NULL;

-- 5) encounter_id must exist in encounter (when referenced)
SELECT 'med_encounter_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM medication m
LEFT JOIN encounter e ON e.encounter_id = m.encounter_id
WHERE m.encounter_id IS NOT NULL AND e.encounter_id IS NULL;

-- 6) practitioner_id must exist in practitioner (when referenced)
SELECT 'med_practitioner_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM medication m
LEFT JOIN practitioner x ON x.practitioner_id = m.practitioner_id
WHERE m.practitioner_id IS NOT NULL AND x.practitioner_id IS NULL;

-- 7) date ordering: dispensing_date >= prescribing_date (when both present)
SELECT 'med_dates_order' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medication
WHERE prescribing_date IS NOT NULL
  AND dispensing_date  IS NOT NULL
  AND dispensing_date < prescribing_date;

-- 8) quantities non-negative (when present)
SELECT 'med_quantity_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medication WHERE quantity IS NOT NULL AND quantity < 0;

SELECT 'med_days_supply_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medication WHERE days_supply IS NOT NULL AND days_supply < 0;

-- 9) terminology: source_code_type must exist (when present)
SELECT 'med_source_code_type_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_type_count
FROM medication m
LEFT JOIN :"terminology_schema".medication_code_type t
  ON m.source_code_type = t.code
WHERE m.source_code_type IS NOT NULL
  AND t.code IS NULL;

-- 10) NDC plausibility (when present and non-blank): digits-only length 10 or 11; not all zeros
WITH ndc AS (
  SELECT REGEXP_REPLACE(ndc_code, '\D', '', 'g') AS ndc_digits
  FROM medication
  WHERE ndc_code IS NOT NULL AND BTRIM(ndc_code) <> ''
)
SELECT 'med_ndc_digits_len_10_or_11' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM ndc
WHERE LENGTH(ndc_digits) NOT IN (10,11);

WITH ndc2 AS (
  SELECT REGEXP_REPLACE(ndc_code, '\D', '', 'g') AS ndc_digits
  FROM medication
  WHERE ndc_code IS NOT NULL AND BTRIM(ndc_code) <> ''
)
SELECT 'med_ndc_not_all_zero' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM ndc2
WHERE ndc_digits ~ '^[0]+$';

-- 11) RxNorm plausibility (when present and non-blank): RxCUI should be digits-only
SELECT 'med_rxnorm_rxCUI_digits' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medication
WHERE rxnorm_code IS NOT NULL
  AND BTRIM(rxnorm_code) <> ''
  AND rxnorm_code !~ '^[0-9]+$';

-- 12) ATC plausibility (when present and non-blank)
-- Accept standard ATC levels:
--  L1:  A
--  L2:  A01
--  L3:  A01A
--  L4:  A01AB
--  L5:  A01AB02
SELECT 'med_atc_pattern_valid' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medication
WHERE atc_code IS NOT NULL
  AND BTRIM(atc_code) <> ''
  AND UPPER(atc_code) !~ '^[A-Z]$|^[A-Z][0-9]{2}$|^[A-Z][0-9]{2}[A-Z]$|^[A-Z][0-9]{2}[A-Z]{2}$|^[A-Z][0-9]{2}[A-Z]{2}[0-9]{2}$';

-- 13) (soft) duplicates for RxNorm-coded rows: (person_id, med_date, rxnorm_code, data_source)
WITH keyed AS (
  SELECT
    person_id,
    COALESCE(dispensing_date, prescribing_date) AS med_date,
    rxnorm_code,
    data_source
  FROM medication
  WHERE person_id IS NOT NULL
    AND rxnorm_code IS NOT NULL
    AND data_source IS NOT NULL
    AND (dispensing_date IS NOT NULL OR prescribing_date IS NOT NULL)
),
dupes AS (
  SELECT person_id, med_date, rxnorm_code, data_source, COUNT(*) AS cnt
  FROM keyed
  GROUP BY 1,2,3,4
  HAVING COUNT(*) > 1
)
SELECT 'med_soft_dupe_person_date_rxnorm' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS dup_group_count
FROM dupes;

-- 14) (soft) duplicates for NDC-coded rows: (person_id, med_date, ndc_digits, data_source)
WITH keyed AS (
  SELECT
    person_id,
    COALESCE(dispensing_date, prescribing_date) AS med_date,
    NULLIF(REGEXP_REPLACE(ndc_code, '\D', '', 'g'),'') AS ndc_digits,
    data_source
  FROM medication
  WHERE person_id IS NOT NULL
    AND ndc_code IS NOT NULL
    AND data_source IS NOT NULL
    AND (dispensing_date IS NOT NULL OR prescribing_date IS NOT NULL)
),
dupes AS (
  SELECT person_id, med_date, ndc_digits, data_source, COUNT(*) AS cnt
  FROM keyed
  WHERE ndc_digits IS NOT NULL
  GROUP BY 1,2,3,4
  HAVING COUNT(*) > 1
)
SELECT 'med_soft_dupe_person_date_ndc' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS dup_group_count
FROM dupes;

-- 15) person/encounter consistency: if both present, person_ids should match
SELECT 'med_person_matches_encounter' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS mismatch_count
FROM medication m
JOIN encounter e ON e.encounter_id = m.encounter_id
WHERE m.encounter_id IS NOT NULL
  AND m.person_id   IS NOT NULL
  AND e.person_id   IS NOT NULL
  AND m.person_id <> e.person_id;

-- 16) tuva_last_run not in the future (when present)
SELECT 'med_tuva_last_run_not_future' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medication
WHERE tuva_last_run IS NOT NULL
  AND tuva_last_run > (NOW()::timestamp without time zone);
