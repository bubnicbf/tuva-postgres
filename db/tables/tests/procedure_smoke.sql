-- db/tests/procedure_smoke.sql
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

-- 1) table has rows
SELECT 'procedure_has_rows' AS test, COUNT(*) > 0 AS pass, COUNT(*) AS row_count
FROM procedure;

-- 2) PK not null
SELECT 'procedure_pk_not_null' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM procedure
WHERE procedure_id IS NULL;

-- 3) PK unique
SELECT 'procedure_pk_unique' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS dup_key_count
FROM (
  SELECT procedure_id FROM procedure GROUP BY procedure_id HAVING COUNT(*) > 1
) d;

-- 4) person_id must exist in patient (when referenced)
SELECT 'procedure_person_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM procedure pr
LEFT JOIN patient p ON p.person_id = pr.person_id
WHERE pr.person_id IS NOT NULL AND p.person_id IS NULL;

-- 5) encounter_id must exist in encounter (when referenced)
SELECT 'procedure_encounter_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM procedure pr
LEFT JOIN encounter e ON e.encounter_id = pr.encounter_id
WHERE pr.encounter_id IS NOT NULL AND e.encounter_id IS NULL;

-- 6) practitioner_id must exist in practitioner (when referenced)
SELECT 'procedure_practitioner_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM procedure pr
LEFT JOIN practitioner x ON x.practitioner_id = pr.practitioner_id
WHERE pr.practitioner_id IS NOT NULL AND x.practitioner_id IS NULL;

-- 7) date logic: procedure_date not in the future (when present)
SELECT 'procedure_date_not_future' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM procedure
WHERE procedure_date IS NOT NULL
  AND procedure_date > CURRENT_DATE;

-- 8) terminology: source_code_type must exist (when present)
SELECT 'procedure_source_code_type_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_type_count
FROM procedure pr
LEFT JOIN :"terminology_schema".procedure_code_type t
  ON pr.source_code_type = t.code
WHERE pr.source_code_type IS NOT NULL
  AND t.code IS NULL;

-- 9) terminology: normalized_code_type must exist (when present)
SELECT 'procedure_normalized_code_type_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_code_type_count
FROM procedure pr
LEFT JOIN :"terminology_schema".procedure_code_type t
  ON pr.normalized_code_type = t.code
WHERE pr.normalized_code_type IS NOT NULL
  AND t.code IS NULL;

-- 10) modifier validity (when present) against hcpcs_modifier
WITH bad_mod AS (
  SELECT 'modifier_1' AS which FROM procedure pr
    LEFT JOIN :"terminology_schema".hcpcs_modifier m ON pr.modifier_1 = m.code
    WHERE pr.modifier_1 IS NOT NULL AND m.code IS NULL
  UNION ALL
  SELECT 'modifier_2' FROM procedure pr
    LEFT JOIN :"terminology_schema".hcpcs_modifier m ON pr.modifier_2 = m.code
    WHERE pr.modifier_2 IS NOT NULL AND m.code IS NULL
  UNION ALL
  SELECT 'modifier_3' FROM procedure pr
    LEFT JOIN :"terminology_schema".hcpcs_modifier m ON pr.modifier_3 = m.code
    WHERE pr.modifier_3 IS NOT NULL AND m.code IS NULL
  UNION ALL
  SELECT 'modifier_4' FROM procedure pr
    LEFT JOIN :"terminology_schema".hcpcs_modifier m ON pr.modifier_4 = m.code
    WHERE pr.modifier_4 IS NOT NULL AND m.code IS NULL
  UNION ALL
  SELECT 'modifier_5' FROM procedure pr
    LEFT JOIN :"terminology_schema".hcpcs_modifier m ON pr.modifier_5 = m.code
    WHERE pr.modifier_5 IS NOT NULL AND m.code IS NULL
)
SELECT 'procedure_modifiers_known' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS unknown_modifier_count
FROM bad_mod;

-- 11) person/encounter consistency: if both present, person_ids should match
SELECT 'procedure_person_matches_encounter' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS mismatch_count
FROM procedure pr
JOIN encounter e ON e.encounter_id = pr.encounter_id
WHERE pr.encounter_id IS NOT NULL
  AND pr.person_id   IS NOT NULL
  AND e.person_id    IS NOT NULL
  AND pr.person_id <> e.person_id;

-- 12) tuva_last_run not in the future (when present)
SELECT 'procedure_tuva_last_run_not_future' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM procedure
WHERE tuva_last_run IS NOT NULL
  AND tuva_last_run > (NOW()::timestamp without time zone);
