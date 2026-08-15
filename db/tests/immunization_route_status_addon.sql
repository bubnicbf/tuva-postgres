-- db/tests/immunization_route_status_addon.sql
-- Expects :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- Route membership
SELECT 'imm_route_unknown_codes' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*)     AS unknown_code_count
FROM (
  SELECT DISTINCT route AS code
  FROM immunization
  WHERE route IS NOT NULL AND btrim(route) <> ''
) r
LEFT JOIN :"terminology_schema".immunization_route_code t
  ON r.code = t.route_code
WHERE t.route_code IS NULL;

-- Status membership
SELECT 'imm_status_unknown_codes' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*)     AS unknown_code_count
FROM (
  SELECT DISTINCT status AS code
  FROM immunization
  WHERE status IS NOT NULL AND btrim(status) <> ''
) s
LEFT JOIN :"terminology_schema".immunization_status t
  ON s.code = t.status_code
WHERE t.status_code IS NULL;
