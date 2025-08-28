-- db/tests/condition_principal_dx_addon.sql
-- Optional add-on: principal diagnosis uniqueness per claim.
-- Expects psql var: :"schema"
SET search_path TO :"schema", public;

-- For each (claim_id, data_source), ensure there is at most one row with condition_rank = 1
WITH per_claim AS (
  SELECT
    claim_id,
    data_source,
    COUNT(*) FILTER (WHERE condition_rank = 1) AS principal_cnt
  FROM condition
  WHERE claim_id IS NOT NULL
    AND data_source IS NOT NULL
  GROUP BY 1,2
  HAVING COUNT(*) FILTER (WHERE condition_rank = 1) > 1
)
SELECT 'condition_principal_dx_unique_per_claim' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS fail_group_count
FROM per_claim;
