-- db/tests/medical_claim_smoke.sql
-- Expects psql vars: :"schema" (and uses :"terminology_schema" for a join)
SET search_path TO :"schema", public;

-- 1) table has rows
SELECT 'medical_claim_has_rows' AS test, COUNT(*) > 0 AS pass, COUNT(*) AS row_count
FROM medical_claim;

-- 2) PK not null
SELECT 'medical_claim_id_not_null' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medical_claim
WHERE medical_claim_id IS NULL;

-- 3) PK unique
SELECT 'medical_claim_id_unique' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS dup_key_count
FROM (
  SELECT medical_claim_id FROM medical_claim GROUP BY medical_claim_id HAVING COUNT(*) > 1
) d;

-- 4) person_id must exist in patient (when referenced)
SELECT 'medical_claim_person_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM medical_claim m
LEFT JOIN patient p ON p.person_id = m.person_id
WHERE m.person_id IS NOT NULL AND p.person_id IS NULL;

-- 5) encounter_id must exist in encounter (when referenced)
SELECT 'medical_claim_encounter_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM medical_claim m
LEFT JOIN encounter e ON e.encounter_id = m.encounter_id
WHERE m.encounter_id IS NOT NULL AND e.encounter_id IS NULL;

-- 6) dates: claim_end_date >= claim_start_date (when both present)
SELECT 'medical_claim_date_order_claim' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medical_claim
WHERE claim_start_date IS NOT NULL
  AND claim_end_date   IS NOT NULL
  AND claim_end_date < claim_start_date;

-- 7) dates: claim_line_end_date >= claim_line_start_date (when both present)
SELECT 'medical_claim_date_order_line' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medical_claim
WHERE claim_line_start_date IS NOT NULL
  AND claim_line_end_date   IS NOT NULL
  AND claim_line_end_date < claim_line_start_date;

-- 8) dates: discharge_date >= admission_date (when both present)
SELECT 'medical_claim_date_order_admission' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medical_claim
WHERE admission_date IS NOT NULL
  AND discharge_date IS NOT NULL
  AND discharge_date < admission_date;

-- 9) (soft) line dates inside claim dates (when all present)
SELECT 'medical_claim_line_within_claim_window' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medical_claim
WHERE claim_start_date      IS NOT NULL
  AND claim_end_date        IS NOT NULL
  AND claim_line_start_date IS NOT NULL
  AND claim_line_end_date   IS NOT NULL
  AND (claim_line_start_date < claim_start_date OR claim_line_end_date > claim_end_date);

-- 10) flags are 0/1 (or NULL)
SELECT 'medical_claim_flags_boolish' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medical_claim
WHERE (in_network_flag  IS NOT NULL AND in_network_flag  NOT IN (0,1))
   OR (enrollment_flag  IS NOT NULL AND enrollment_flag  NOT IN (0,1));

-- 11) money amounts are non-negative (when present)
SELECT 'medical_claim_paid_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medical_claim WHERE paid_amount       IS NOT NULL AND paid_amount       < 0;

SELECT 'medical_claim_allowed_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medical_claim WHERE allowed_amount    IS NOT NULL AND allowed_amount    < 0;

SELECT 'medical_claim_charge_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medical_claim WHERE charge_amount     IS NOT NULL AND charge_amount     < 0;

SELECT 'medical_claim_coinsurance_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medical_claim WHERE coinsurance_amount IS NOT NULL AND coinsurance_amount < 0;

SELECT 'medical_claim_copayment_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medical_claim WHERE copayment_amount  IS NOT NULL AND copayment_amount  < 0;

SELECT 'medical_claim_deductible_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medical_claim WHERE deductible_amount IS NOT NULL AND deductible_amount < 0;

SELECT 'medical_claim_total_cost_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medical_claim WHERE total_cost_amount IS NOT NULL AND total_cost_amount < 0;

SELECT 'medical_claim_units_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medical_claim WHERE service_unit_quantity IS NOT NULL AND service_unit_quantity < 0;

-- 12) money relationships (soft sanity): paid <= allowed <= charge (when all present)
SELECT 'medical_claim_money_relationships' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medical_claim
WHERE paid_amount    IS NOT NULL
  AND allowed_amount IS NOT NULL
  AND charge_amount  IS NOT NULL
  AND (paid_amount > allowed_amount OR allowed_amount > charge_amount);

-- 13) (soft) member cost share <= allowed (when all present)
SELECT 'medical_claim_costshare_le_allowed' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM (
  SELECT
    medical_claim_id,
    COALESCE(coinsurance_amount,0) + COALESCE(copayment_amount,0) + COALESCE(deductible_amount,0) AS cost_share,
    allowed_amount
  FROM medical_claim
  WHERE allowed_amount IS NOT NULL
) s
WHERE cost_share > allowed_amount;

-- 14) claim_line_number positive (when present)
SELECT 'medical_claim_line_number_positive' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medical_claim
WHERE claim_line_number IS NOT NULL AND claim_line_number <= 0;

-- 15) (soft) uniqueness of (claim_id, claim_line_number, data_source)
WITH dupes AS (
  SELECT
    COALESCE(claim_id,'')          AS claim_id_n,
    COALESCE(claim_line_number::text,'') AS claim_line_number_n,
    COALESCE(data_source,'')       AS data_source_n,
    COUNT(*) AS cnt
  FROM medical_claim
  GROUP BY 1,2,3
  HAVING COUNT(*) > 1
)
SELECT 'medical_claim_line_unique_tuple' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS dup_group_count
FROM dupes;

-- 16) (soft) person_id on claim should match person_id of the linked encounter (when both present)
SELECT 'medical_claim_person_matches_encounter' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS mismatch_count
FROM medical_claim m
JOIN encounter e ON e.encounter_id = m.encounter_id
WHERE m.encounter_id IS NOT NULL
  AND m.person_id   IS NOT NULL
  AND e.person_id   IS NOT NULL
  AND m.person_id <> e.person_id;

-- 17) tuva_last_run not in the future (when present)
SELECT 'medical_claim_tuva_last_run_not_future' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM medical_claim
WHERE tuva_last_run IS NOT NULL
  AND tuva_last_run > (NOW()::timestamp without time zone);

-- 18) NEW: claim_type must exist in terminology (when present)
SELECT 'medical_claim_claim_type_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_count
FROM medical_claim m
LEFT JOIN :"terminology_schema".claim_type t
  ON m.claim_type = t.claim_type
WHERE m.claim_type IS NOT NULL
  AND t.claim_type IS NULL;
