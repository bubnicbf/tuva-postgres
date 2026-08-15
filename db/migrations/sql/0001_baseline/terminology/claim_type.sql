-- db/terminology/claim_type.sql
-- Terminology: Claim Type (single-column value set).
-- Uses psql var :"terminology_schema"

-- 1) Ensure table exists
CREATE TABLE IF NOT EXISTS :"terminology_schema".claim_type (
  claim_type varchar
);

-- 2) Ensure target column exists (for legacy schemas)
ALTER TABLE :"terminology_schema".claim_type
  ADD COLUMN IF NOT EXISTS claim_type varchar;

-- 3) Backfill from a legacy 'code' column if present
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = :'terminology_schema'
      AND table_name   = 'claim_type'
      AND column_name  = 'code'
  ) THEN
    EXECUTE format('UPDATE %I.claim_type SET claim_type = code WHERE claim_type IS NULL', :'terminology_schema');
  END IF;
END$$;

-- 4) Add PK on claim_type if not already present
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint c
    JOIN pg_class r ON r.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = r.relnamespace
    WHERE c.contype = 'p'
      AND c.conname = 'claim_type_pkey'
      AND r.relname = 'claim_type'
      AND n.nspname = :'terminology_schema'
  ) THEN
    ALTER TABLE :"terminology_schema".claim_type
      ADD CONSTRAINT claim_type_pkey PRIMARY KEY (claim_type);
  END IF;
END$$;

-- 5) (Optional) drop legacy columns if present (uncomment when ready)
-- ALTER TABLE :"terminology_schema".claim_type
--   DROP COLUMN IF EXISTS code,
--   DROP COLUMN IF EXISTS description,
--   DROP COLUMN IF EXISTS display,
--   DROP COLUMN IF EXISTS system,
--   DROP COLUMN IF EXISTS claim_type_description;

-- 6) Comments
COMMENT ON TABLE  :"terminology_schema".claim_type IS 'Terminology: Claim Type (single column).';
COMMENT ON COLUMN :"terminology_schema".claim_type.claim_type IS 'The type of the claim.';
