-- db/tests/encounter_type_addon.sql
-- Soft checks for encounter.encounter_type against terminology.encounter_type.
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- 1) When encounter_group is provided: (encounter_group, encounter_type) pair must exist
WITH missing AS (
  SELECT DISTINCT e.encounter_group, e.encounter_type
  FROM encounter e
  LEFT JOIN :"terminology_schema".encounter_type t
    ON e.encounter_group = t.encounter_group
   AND e.encounter_type  = t.encounter_type
  WHERE e.encounter_group IS NOT NULL AND btrim(e.encounter_group) <> ''
    AND e.encounter_type  IS NOT NULL AND btrim(e.encounter_type)  <> ''
    AND t.encounter_type IS NULL
)
SELECT 'encounter_type_unknown_pair' AS test,
       (SELECT COUNT(*) FROM missing) = 0 AS pass,
       (SELECT COUNT(*) FROM missing)     AS unknown_pair_count;

-- 2) When encounter_group is missing: type must exist in any group (type-only membership)
WITH missing AS (
  SELECT DISTINCT e.encounter_type
  FROM encounter e
  WHERE (e.encounter_group IS NULL OR btrim(e.encounter_group) = '')
    AND e.encounter_type IS NOT NULL AND btrim(e.encounter_type) <> ''
    AND NOT EXISTS (
      SELECT 1
      FROM :"terminology_schema".encounter_type t
      WHERE t.encounter_type = e.encounter_type
    )
)
SELECT 'encounter_type_unknown_type_when_no_group' AS test,
       (SELECT COUNT(*) FROM missing) = 0 AS pass,
       (SELECT COUNT(*) FROM missing)     AS unknown_type_count;
