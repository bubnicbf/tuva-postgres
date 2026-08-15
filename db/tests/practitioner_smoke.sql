-- db/tests/practitioner_smoke.sql
-- Expects psql var: :"schema"
SET search_path TO :"schema", public;

-- 1) table has rows
SELECT 'practitioner_has_rows' AS test, COUNT(*) > 0 AS pass, COUNT(*) AS row_count
FROM practitioner;

-- 2) PK not null
SELECT 'practitioner_id_not_null' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM practitioner
WHERE practitioner_id IS NULL;

-- 3) PK unique
SELECT 'practitioner_id_unique' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS dup_key_count
FROM (
  SELECT practitioner_id
  FROM practitioner
  GROUP BY practitioner_id
  HAVING COUNT(*) > 1
) d;

-- 4) basic name presence: at least one of first/last must be non-empty
SELECT 'practitioner_name_present' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM practitioner
WHERE COALESCE(NULLIF(BTRIM(provider_first_name), ''), NULL) IS NULL
  AND COALESCE(NULLIF(BTRIM(provider_last_name),  ''), NULL) IS NULL;

-- 5) NPI format (when present): exactly 10 digits after stripping non-digits
SELECT 'practitioner_npi_10_digits' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM practitioner
WHERE npi IS NOT NULL
  AND LENGTH(REGEXP_REPLACE(npi, '\D', '', 'g')) <> 10;

-- 6) NPI not all zeros (when present)
SELECT 'practitioner_npi_not_all_zero' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM practitioner
WHERE npi IS NOT NULL
  AND REGEXP_REPLACE(npi, '\D', '', 'g') = '0000000000';

-- 7) tuva_last_run not in the future (when present)
SELECT 'practitioner_tuva_last_run_not_future' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM practitioner
WHERE tuva_last_run IS NOT NULL
  AND tuva_last_run > (NOW()::timestamp without time zone);

-- 8) NPI Luhn check (when present)
SELECT 'practitioner_npi_luhn_valid' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM practitioner
WHERE npi IS NOT NULL
  AND (NOT :"schema".is_valid_npi(npi));
