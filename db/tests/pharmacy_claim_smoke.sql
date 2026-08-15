-- db/tests/pharmacy_claim_smoke.sql
-- Expects psql var: :"schema"
SET search_path TO :"schema", public;

-- 1) table has rows
SELECT 'pharmacy_claim_has_rows' AS test, COUNT(*) > 0 AS pass, COUNT(*) AS row_count
FROM pharmacy_claim;

-- 2) PK not null
SELECT 'pharmacy_claim_id_not_null' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM pharmacy_claim
WHERE pharmacy_claim_id IS NULL;

-- 3) PK unique
SELECT 'pharmacy_claim_id_unique' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS dup_key_count
FROM (
  SELECT pharmacy_claim_id FROM pharmacy_claim GROUP BY pharmacy_claim_id HAVING COUNT(*) > 1
) d;

-- 4) person_id must exist in patient (when referenced)
SELECT 'pharmacy_claim_person_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM pharmacy_claim r
LEFT JOIN patient p ON p.person_id = r.person_id
WHERE r.person_id IS NOT NULL AND p.person_id IS NULL;

-- 5) paid_date should not precede dispensing_date (when both present)
SELECT 'pharmacy_paid_after_dispense' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM pharmacy_claim
WHERE dispensing_date IS NOT NULL
  AND paid_date       IS NOT NULL
  AND paid_date < dispensing_date;

-- 6) flags are 0/1 (or NULL)
SELECT 'pharmacy_flags_boolish' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM pharmacy_claim
WHERE (in_network_flag IS NOT NULL AND in_network_flag NOT IN (0,1))
   OR (enrollment_flag IS NOT NULL AND enrollment_flag NOT IN (0,1));

-- 7) quantities non-negative (when present)
SELECT 'pharmacy_quantities_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM pharmacy_claim
WHERE (quantity    IS NOT NULL AND quantity    < 0)
   OR (days_supply IS NOT NULL AND days_supply < 0)
   OR (refills     IS NOT NULL AND refills     < 0);

-- 8) money amounts non-negative (when present)
SELECT 'pharmacy_paid_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM pharmacy_claim WHERE paid_amount IS NOT NULL AND paid_amount < 0;

SELECT 'pharmacy_allowed_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM pharmacy_claim WHERE allowed_amount IS NOT NULL AND allowed_amount < 0;

SELECT 'pharmacy_charge_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM pharmacy_claim WHERE charge_amount IS NOT NULL AND charge_amount < 0;

SELECT 'pharmacy_coinsurance_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM pharmacy_claim WHERE coinsurance_amount IS NOT NULL AND coinsurance_amount < 0;

SELECT 'pharmacy_copayment_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM pharmacy_claim WHERE copayment_amount IS NOT NULL AND copayment_amount < 0;

SELECT 'pharmacy_deductible_nonneg' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM pharmacy_claim WHERE deductible_amount IS NOT NULL AND deductible_amount < 0;

-- 9) money relationships (soft sanity): paid <= allowed <= charge (when all present)
SELECT 'pharmacy_money_relationships' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM pharmacy_claim
WHERE paid_amount    IS NOT NULL
  AND allowed_amount IS NOT NULL
  AND charge_amount  IS NOT NULL
  AND (paid_amount > allowed_amount OR allowed_amount > charge_amount);

-- 10) (soft) member cost share <= allowed (when all present)
SELECT 'pharmacy_costshare_le_allowed' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM (
  SELECT
    pharmacy_claim_id,
    COALESCE(coinsurance_amount,0) + COALESCE(copayment_amount,0) + COALESCE(deductible_amount,0) AS cost_share,
    allowed_amount
  FROM pharmacy_claim
  WHERE allowed_amount IS NOT NULL
) s
WHERE cost_share > allowed_amount;

-- 11) claim_line_number positive (when present)
SELECT 'pharmacy_line_number_positive' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM pharmacy_claim
WHERE claim_line_number IS NOT NULL AND claim_line_number <= 0;

-- 12) (soft) uniqueness of (claim_id, claim_line_number, data_source)
WITH dupes AS (
  SELECT
    COALESCE(claim_id,'')                AS claim_id_n,
    COALESCE(claim_line_number::text,'') AS claim_line_number_n,
    COALESCE(data_source,'')             AS data_source_n,
    COUNT(*) AS cnt
  FROM pharmacy_claim
  GROUP BY 1,2,3
  HAVING COUNT(*) > 1
)
SELECT 'pharmacy_line_unique_tuple' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS dup_group_count
FROM dupes;

-- 13) NDC plausibility: digits-only length should be 10 or 11 (when present and non-blank)
WITH ndc AS (
  SELECT REGEXP_REPLACE(ndc_code, '\D', '', 'g') AS ndc_digits
  FROM pharmacy_claim
  WHERE ndc_code IS NOT NULL AND BTRIM(ndc_code) <> ''
)
SELECT 'pharmacy_ndc_digits_len' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM ndc
WHERE LENGTH(ndc_digits) NOT IN (10,11);

-- Not all zeros
WITH ndc AS (
  SELECT REGEXP_REPLACE(ndc_code, '\D', '', 'g') AS ndc_digits
  FROM pharmacy_claim
  WHERE ndc_code IS NOT NULL AND BTRIM(ndc_code) <> ''
)
SELECT 'pharmacy_ndc_not_all_zero' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM ndc
WHERE ndc_digits ~ '^[0]+$';

-- 14) (soft) member mapping via crosswalk: if a mapping exists for member_id+payer+plan, person_ids should match
SELECT 'pharmacy_member_map_person_match' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS mismatch_count
FROM pharmacy_claim r
JOIN person_id_crosswalk x
  ON COALESCE(r.member_id,'') = COALESCE(x.member_id,'')
 AND COALESCE(r.payer,'')     = COALESCE(x.payer,'')
 AND COALESCE(r.plan,'')      = COALESCE(x.plan,'')
WHERE r.member_id IS NOT NULL
  AND r.person_id IS NOT NULL
  AND x.person_id IS NOT NULL
  AND r.person_id <> x.person_id;

-- 15) tuva_last_run: if present, should not be blank (string in this model)
SELECT 'pharmacy_tuva_last_run_not_blank' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM pharmacy_claim
WHERE tuva_last_run IS NOT NULL AND BTRIM(tuva_last_run) = '';
