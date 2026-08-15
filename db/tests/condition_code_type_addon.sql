-- db/tests/condition_code_type_addon.sql
-- Soft check: distinct condition code types must exist in terminology.code_type.
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

WITH missing AS (
  SELECT DISTINCT ct AS code_type
  FROM (
    SELECT source_code_type     AS ct FROM condition WHERE source_code_type     IS NOT NULL
    UNION
    SELECT normalized_code_type AS ct FROM condition WHERE normalized_code_type IS NOT NULL
  ) u
  LEFT JOIN :"terminology_schema".code_type t ON u.ct = t.code_type
  WHERE t.code_type IS NULL
)
SELECT 'condition_unknown_code_types' AS test,
       (SELECT COUNT(*) FROM missing) = 0 AS pass,
       (SELECT COUNT(*) FROM missing)     AS unknown_code_type_count;
