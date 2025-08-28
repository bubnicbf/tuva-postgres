-- db/terminology/cvx.sql
-- Terminology: CDC CVX vaccine codes (code → short/long description).
-- Uses psql var :"terminology_schema"

-- 1) Ensure target table exists (final shape)
CREATE TABLE IF NOT EXISTS :"terminology_schema".cvx (
  cvx               varchar PRIMARY KEY,  -- CVX code (digits in CDC set, but keep varchar)
  short_description varchar,
  long_description  varchar
);

-- 2) Align columns for older installs (safe to re-run)
ALTER TABLE :"terminology_schema".cvx
  ADD COLUMN IF NOT EXISTS cvx               varchar,
  ADD COLUMN IF NOT EXISTS short_description varchar,
  ADD COLUMN IF NOT EXISTS long_description  varchar;

-- 3) Migrate legacy column names if present
--    cvx_code  -> cvx
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = :'terminology_schema' AND table_name = 'cvx' AND column_name = 'cvx_code'
  )
  AND NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = :'terminology_schema' AND table_name = 'cvx' AND column_name = 'cvx'
  ) THEN
    ALTER TABLE :"terminology_schema".cvx RENAME COLUMN cvx_code TO cvx;
  END IF;
END$$;

--    full_description -> long_description (rename or copy then drop)
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = :'terminology_schema' AND table_name = 'cvx' AND column_name = 'full_description'
  )
  AND NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = :'terminology_schema' AND table_name = 'cvx' AND column_name = 'long_description'
  ) THEN
    ALTER TABLE :"terminology_schema".cvx RENAME COLUMN full_description TO long_description;
  ELSIF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = :'terminology_schema' AND table_name = 'cvx' AND column_name = 'full_description'
  )
  AND EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = :'terminology_schema' AND table_name = 'cvx' AND column_name = 'long_description'
  ) THEN
    EXECUTE format('UPDATE %I.cvx SET long_description = COALESCE(long_description, full_description)', :'terminology_schema');
    ALTER TABLE :"terminology_schema".cvx DROP COLUMN IF EXISTS full_description;
  END IF;
END$$;

-- 4) Drop legacy extras if they exist (keep table lean)
ALTER TABLE :"terminology_schema".cvx
  DROP COLUMN IF EXISTS status,
  DROP COLUMN IF EXISTS notes,
  DROP COLUMN IF EXISTS cvx_version,
  DROP COLUMN IF EXISTS effective_start_date,
  DROP COLUMN IF EXISTS effective_end_date;

-- 5) Ensure PK is on (cvx)
DO $$
DECLARE
  pk_on_cvx boolean := FALSE;
  pk_name   text;
BEGIN
  SELECT (COUNT(*) > 0)
  INTO pk_on_cvx
  FROM pg_constraint c
  JOIN pg_class r ON r.oid = c.conrelid
  JOIN pg_namespace n ON n.oid = r.relnamespace
  JOIN pg_attribute a ON a.attrelid = r.oid AND a.attnum = ANY(c.conkey)
  WHERE c.contype = 'p'
    AND r.relname = 'cvx'
    AND n.nspname = :'terminology_schema'
    AND a.attname = 'cvx';

  IF NOT pk_on_cvx THEN
    -- drop any existing primary key (e.g., on cvx_code) and re-add on cvx
    SELECT c.conname
      INTO pk_name
    FROM pg_constraint c
    JOIN pg_class r ON r.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = r.relnamespace
    WHERE c.contype = 'p'
      AND r.relname = 'cvx'
      AND n.nspname = :'terminology_schema';

    IF pk_name IS NOT NULL THEN
      EXECUTE format('ALTER TABLE %I.cvx DROP CONSTRAINT %I', :'terminology_schema', pk_name);
    END IF;

    EXECUTE format('ALTER TABLE %I.cvx ADD CONSTRAINT cvx_pkey PRIMARY KEY (cvx)', :'terminology_schema');
  END IF;
END$$;

-- 6) Helpful index for searches
CREATE INDEX IF NOT EXISTS cvx_short_desc_idx ON :"terminology_schema".cvx (short_description);

-- 7) Comments
COMMENT ON TABLE  :"terminology_schema".cvx IS 'CDC CVX vaccine codes (cvx → short/long description).';
COMMENT ON COLUMN :"terminology_schema".cvx.cvx               IS 'CVX code, unique identifier for vaccines.';
COMMENT ON COLUMN :"terminology_schema".cvx.short_description IS 'Short description of the vaccine.';
COMMENT ON COLUMN :"terminology_schema".cvx.long_description  IS 'Long description of the vaccine.';
