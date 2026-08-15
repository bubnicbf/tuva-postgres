-- db/tests/encounter_smoke.sql
-- Expects psql vars: :"schema"
SET search_path TO :"schema", public;

-- 1) table has rows
SELECT 'encounter_has_rows' AS test, COUNT(*) > 0 AS pass, COUNT(*) AS row_count
FROM encounter;

-- 2) PK not null
SELECT 'encounter_id_not_null' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM encounter WHERE encounter_id IS NULL;

-- 3) PK unique
SELECT 'encounter_id_unique' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS dup_key_count
FROM (
  SELECT encounter_id FROM encounter GROUP BY encounter_id HAVING COUNT(*) > 1
) d;

-- 4) person_id must exist in patient (when referenced)
SELECT 'encounter_person_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM encounter e
LEFT JOIN patient p ON p.person_id = e.person_id
WHERE e.person_id IS NOT NULL AND p.person_id IS NULL;

-- 5) attending_provider_id must exist in practitioner (when referenced)
SELECT 'encounter_attending_pr_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM encounter e
LEFT JOIN practitioner pr ON pr.practitioner_id = e.attending_provider_id
WHERE e.attending_provider_id IS NOT NULL AND pr.practitioner_id IS NULL;

-- 6) end date >= start date (when both present)
SELECT 'encounter_date_order' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM encounter
WHERE encounter_start_date IS NOT NULL
  AND encounter_end_date   IS NOT NULL
  AND encounter_end_date < encounter_start_date;

-- 7) length_of_stay non-negative
SELECT 'encounter_los_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM encounter
WHERE length_of_stay IS NOT NULL AND length_of_stay < 0;

-- 8) length_of_stay matches (end - start) when both dates present
SELECT 'encounter_los_matches_dates' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM encounter
WHERE encounter_start_date IS NOT NULL
  AND encounter_end_date   IS NOT NULL
  AND length_of_stay IS NOT NULL
  AND length_of_stay <> (encounter_end_date - encounter_start_date);

-- 9) boolean-ish flags only 0/1 (or NULL)
WITH bad AS (
  SELECT 'observation_flag' AS flag FROM encounter WHERE observation_flag IS NOT NULL AND observation_flag NOT IN (0,1)
  UNION ALL SELECT 'lab_flag' FROM encounter WHERE lab_flag IS NOT NULL AND lab_flag NOT IN (0,1)
  UNION ALL SELECT 'dme_flag' FROM encounter WHERE dme_flag IS NOT NULL AND dme_flag NOT IN (0,1)
  UNION ALL SELECT 'ambulance_flag' FROM encounter WHERE ambulance_flag IS NOT NULL AND ambulance_flag NOT IN (0,1)
  UNION ALL SELECT 'pharmacy_flag' FROM encounter WHERE pharmacy_flag IS NOT NULL AND pharmacy_flag NOT IN (0,1)
  UNION ALL SELECT 'ed_flag' FROM encounter WHERE ed_flag IS NOT NULL AND ed_flag NOT IN (0,1)
  UNION ALL SELECT 'delivery_flag' FROM encounter WHERE delivery_flag IS NOT NULL AND delivery_flag NOT IN (0,1)
  UNION ALL SELECT 'newborn_flag' FROM encounter WHERE newborn_flag IS NOT NULL AND newborn_flag NOT IN (0,1)
  UNION ALL SELECT 'nicu_flag' FROM encounter WHERE nicu_flag IS NOT NULL AND nicu_flag NOT IN (0,1)
  UNION ALL SELECT 'snf_part_b_flag' FROM encounter WHERE snf_part_b_flag IS NOT NULL AND snf_part_b_flag NOT IN (0,1)
)
SELECT 'encounter_flags_boolish' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM bad;

-- 10) money amounts non-negative (when present)
SELECT 'encounter_paid_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM encounter WHERE paid_amount    IS NOT NULL AND paid_amount    < 0;

SELECT 'encounter_allowed_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM encounter WHERE allowed_amount IS NOT NULL AND allowed_amount < 0;

SELECT 'encounter_charge_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM encounter WHERE charge_amount IS NOT NULL AND charge_amount  < 0;

-- 11) money relationships (soft sanity): paid <= allowed <= charge (when all present)
SELECT 'encounter_money_relationships' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM encounter
WHERE paid_amount    IS NOT NULL
  AND allowed_amount IS NOT NULL
  AND charge_amount  IS NOT NULL
  AND (paid_amount > allowed_amount OR allowed_amount > charge_amount);

-- 12) claim counts non-negative (when present)
SELECT 'encounter_claim_counts_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM encounter
WHERE (claim_count      IS NOT NULL AND claim_count      < 0)
   OR (inst_claim_count IS NOT NULL AND inst_claim_count < 0)
   OR (prof_claim_count IS NOT NULL AND prof_claim_count < 0);
