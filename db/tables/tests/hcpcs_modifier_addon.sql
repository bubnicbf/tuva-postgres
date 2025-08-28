-- Soft checks for HCPCS modifiers in medical_claim.
-- Expects :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

WITH unpivot AS (
  SELECT unnest(ARRAY[
    NULLIF(btrim(hcpcs_modifier_1), ''),
    NULLIF(btrim(hcpcs_modifier_2), ''),
    NULLIF(btrim(hcpcs_modifier_3), ''),
    NULLIF(btrim(hcpcs_modifier_4), ''),
    NULLIF(btrim(hcpcs_modifier_5), '')
  ]) AS modifier
  FROM medical_claim
),
mods AS (
  SELECT DISTINCT upper(modifier) AS modifier
  FROM unpivot
  WHERE modifier IS NOT NULL
)

-- 1) Unknown modifiers (distinct)
SELECT 'mc_hcpcs_modifier_unknown_codes' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*)     AS unknown_code_count
FROM mods m
LEFT JOIN :"terminology_schema".hcpcs_modifier t
  ON m.modifier = t.modifier
WHERE t.modifier IS NULL;

-- 2) Format check: typically two alphanumeric characters
SELECT 'mc_hcpcs_modifier_format' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*)     AS bad_format_count
FROM mods
WHERE modifier !~ '^[A-Z0-9]{2}$';
