-- db/tests/eligibility_smoke.sql
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- 1) table has rows
SELECT 'elig_has_rows' AS test, COUNT(*) > 0 AS pass, COUNT(*) AS row_count
FROM eligibility;

-- 2) PK not null
SELECT 'elig_pk_not_null' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM eligibility
WHERE eligibility_id IS NULL;

-- 3) PK unique
SELECT 'elig_pk_unique' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS dup_key_count
FROM (
  SELECT eligibility_id FROM eligibility GROUP BY eligibility_id HAVING COUNT(*) > 1
) d;

-- 4) person_id must exist in patient (when referenced)
SELECT 'elig_person_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM eligibility e
LEFT JOIN patient p ON p.person_id = e.person_id
WHERE e.person_id IS NOT NULL AND p.person_id IS NULL;

-- 5) enrollment_end_date >= enrollment_start_date (when both present)
SELECT 'elig_dates_order' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM eligibility
WHERE enrollment_start_date IS NOT NULL
  AND enrollment_end_date   IS NOT NULL
  AND enrollment_end_date < enrollment_start_date;

-- 6) death after birth (when both present)
SELECT 'elig_death_after_birth' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM eligibility
WHERE birth_date IS NOT NULL
  AND death_date IS NOT NULL
  AND death_date < birth_date;

-- 7) (soft) non-overlapping periods per member_id+payer+plan+data_source
WITH spans AS (
  SELECT
    eligibility_id,
    member_id, payer, plan, data_source,
    enrollment_start_date AS s,
    enrollment_end_date   AS e
  FROM eligibility
  WHERE member_id IS NOT NULL
    AND payer     IS NOT NULL
    AND plan      IS NOT NULL
    AND data_source IS NOT NULL
),
seq AS (
  SELECT
    *,
    LEAD(s) OVER (PARTITION BY member_id, payer, plan, data_source ORDER BY s NULLS LAST) AS next_s
  FROM spans
)
SELECT 'elig_non_overlapping_member_plan' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS overlap_row_count
FROM seq
WHERE s IS NOT NULL
  AND e IS NOT NULL
  AND next_s IS NOT NULL
  AND next_s <= e;  -- treat end date as inclusive

-- 8) (soft) duplicate rows on identifying tuple
WITH dupes AS (
  SELECT
    COALESCE(person_id,'') AS person_id_n,
    COALESCE(member_id,'') AS member_id_n,
    COALESCE(subscriber_id,'') AS subscriber_id_n,
    COALESCE(payer,'') AS payer_n,
    COALESCE(plan,'')  AS plan_n,
    COALESCE(enrollment_start_date::text,'') AS s_n,
    COALESCE(enrollment_end_date::text,'')   AS e_n,
    COALESCE(data_source,'') AS data_source_n,
    COUNT(*) AS cnt
  FROM eligibility
  GROUP BY 1,2,3,4,5,6,7,8
  HAVING COUNT(*) > 1
)
SELECT 'elig_soft_duplicate_tuple' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS dup_group_count
FROM dupes;

-- 9) terminology presence checks (when codes present)
-- payer_type exists
SELECT 'elig_payer_type_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_count
FROM eligibility e
LEFT JOIN :"terminology_schema".payer_type t ON e.payer_type = t.code
WHERE e.payer_type IS NOT NULL AND t.code IS NULL;

-- original_reason_entitlement_code exists
SELECT 'elig_orec_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_count
FROM eligibility e
LEFT JOIN :"terminology_schema".original_reason_entitlement_code t ON e.original_reason_entitlement_code = t.code
WHERE e.original_reason_entitlement_code IS NOT NULL AND t.code IS NULL;

-- dual_status_code exists
SELECT 'elig_dual_status_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_count
FROM eligibility e
LEFT JOIN :"terminology_schema".dual_status_code t ON e.dual_status_code = t.code
WHERE e.dual_status_code IS NOT NULL AND t.code IS NULL;

-- medicare_status_code exists
SELECT 'elig_medicare_status_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_count
FROM eligibility e
LEFT JOIN :"terminology_schema".medicare_status_code t ON e.medicare_status_code = t.code
WHERE e.medicare_status_code IS NOT NULL AND t.code IS NULL;

-- 10) FIPS/state plausibility (soft)
SELECT 'elig_fips_state_code_len2_digits' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM eligibility
WHERE fips_state_code IS NOT NULL
  AND (LENGTH(fips_state_code) <> 2 OR fips_state_code !~ '^[0-9]{2}$');

SELECT 'elig_state_abbrev_len2_letters' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM eligibility
WHERE fips_state_abbreviation IS NOT NULL
  AND (LENGTH(fips_state_abbreviation) <> 2 OR fips_state_abbreviation !~ '^[A-Za-z]{2}$');

-- 11) Crosswalk consistency (soft): if a mapping exists for member_id+payer+plan, person_ids should match
SELECT 'elig_member_map_person_match' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS mismatch_count
FROM eligibility e
JOIN person_id_crosswalk x
  ON COALESCE(e.member_id,'') = COALESCE(x.member_id,'')
 AND COALESCE(e.payer,'')     = COALESCE(x.payer,'')
 AND COALESCE(e.plan,'')      = COALESCE(x.plan,'')
WHERE e.member_id IS NOT NULL
  AND e.person_id IS NOT NULL
  AND x.person_id IS NOT NULL
  AND e.person_id <> x.person_id;

-- 12) tuva_last_run: if present, should not be blank (string in this model)
SELECT 'elig_tuva_last_run_not_blank' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM eligibility
WHERE tuva_last_run IS NOT NULL AND BTRIM(tuva_last_run) = '';
