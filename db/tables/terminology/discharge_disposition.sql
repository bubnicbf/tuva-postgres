-- db/terminology/discharge_disposition.sql
-- Terminology: Discharge Disposition (code → description).
-- Uses psql var :"terminology_schema" supplied by the wrapper.

-- 1) Create table (target shape)
CREATE TABLE IF NOT EXISTS :"terminology_schema".discharge_disposition (
  discharge_disposition_code         varchar PRIMARY KEY,
  discharge_disposition_description  varchar
);

-- 2) Align columns for older installs (safe to re-run)
ALTER TABLE :"terminology_schema".discharge_disposition
  ADD COLUMN IF NOT EXISTS discharge_disposition_code        varchar,
  ADD COLUMN IF NOT EXISTS discharge_disposition_description varchar;

-- 3) Ensure primary key on (discharge_disposition_code) if legacy table lacked one
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint c
    JOIN pg_class r ON r.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = r.relnamespace
    WHERE c.contype = 'p'
      AND r.relname = 'discharge_disposition'
      AND n.nspname = :'terminology_schema'
  ) THEN
    EXECUTE format('ALTER TABLE %I.discharge_disposition ADD CONSTRAINT discharge_disposition_pkey PRIMARY KEY (discharge_disposition_code)', :'terminology_schema');
  END IF;
END$$;

-- 4) Helpful index for text searches
CREATE INDEX IF NOT EXISTS discharge_disposition_desc_idx
  ON :"terminology_schema".discharge_disposition (discharge_disposition_description);

-- 5) Documentation
COMMENT ON TABLE  :"terminology_schema".discharge_disposition IS
  'Terminology: discharge disposition lookup (code → description).';
COMMENT ON COLUMN :"terminology_schema".discharge_disposition.discharge_disposition_code IS
  'The code representing the discharge disposition.';
COMMENT ON COLUMN :"terminology_schema".discharge_disposition.discharge_disposition_description IS
  'A description of the discharge disposition.';
