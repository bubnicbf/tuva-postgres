-- db/terminology/race.sql
-- Terminology: Race (code → description).
-- Uses psql var :"terminology_schema" supplied by the wrapper.

-- 1) Create table to target shape
CREATE TABLE IF NOT EXISTS :"terminology_schema".race (
  code        varchar PRIMARY KEY,
  description varchar
);

-- 2) Align columns for legacy installs (safe to re-run)
ALTER TABLE :"terminology_schema".race
  ADD COLUMN IF NOT EXISTS code        varchar,
  ADD COLUMN IF NOT EXISTS description varchar;

-- 3) Ensure PK on (code) if a legacy table lacked one
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint c
    JOIN pg_class r ON r.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = r.relnamespace
    WHERE c.contype = 'p'
      AND r.relname = 'race'
      AND n.nspname = :'terminology_schema'
  ) THEN
    EXECUTE format('ALTER TABLE %I.race ADD CONSTRAINT race_pkey PRIMARY KEY (code)', :'terminology_schema');
  END IF;
END$$;

-- 4) Optional helper index for lookups by description
CREATE INDEX IF NOT EXISTS race_description_idx
  ON :"terminology_schema".race (description);

-- 5) Documentation
COMMENT ON TABLE  :"terminology_schema".race IS
  'Terminology: race codes and descriptions.';
COMMENT ON COLUMN :"terminology_schema".race.code IS
  'The code representing the race.';
COMMENT ON COLUMN :"terminology_schema".race.description IS
  'A description of the race.';
