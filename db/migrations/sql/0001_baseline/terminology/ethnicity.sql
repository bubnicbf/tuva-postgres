-- db/terminology/ethnicity.sql
-- Terminology: Ethnicity (code → description).
-- Uses psql var :"terminology_schema" supplied by the wrapper.

-- 1) Create table to target shape
CREATE TABLE IF NOT EXISTS :"terminology_schema".ethnicity (
  code        varchar PRIMARY KEY,
  description varchar
);

-- 2) Align columns for legacy installs (safe to re-run)
ALTER TABLE :"terminology_schema".ethnicity
  ADD COLUMN IF NOT EXISTS code        varchar,
  ADD COLUMN IF NOT EXISTS description varchar;

-- 3) Ensure PK on (code) for legacy tables that lacked one
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint c
    JOIN pg_class r ON r.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = r.relnamespace
    WHERE c.contype = 'p'
      AND r.relname = 'ethnicity'
      AND n.nspname = :'terminology_schema'
  ) THEN
    EXECUTE format('ALTER TABLE %I.ethnicity ADD CONSTRAINT ethnicity_pkey PRIMARY KEY (code)', :'terminology_schema');
  END IF;
END$$;

-- 4) Optional helper index for lookups by description
CREATE INDEX IF NOT EXISTS ethnicity_description_idx
  ON :"terminology_schema".ethnicity (description);

-- 5) Documentation
COMMENT ON TABLE  :"terminology_schema".ethnicity IS
  'Terminology: ethnicity codes and descriptions.';
COMMENT ON COLUMN :"terminology_schema".ethnicity.code IS
  'The code representing the ethnicity.';
COMMENT ON COLUMN :"terminology_schema".ethnicity.description IS
  'A description of the ethnicity.';
