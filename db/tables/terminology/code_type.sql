-- db/terminology/code_type.sql
-- Terminology: generic code type value set (single column).
-- Uses psql var :"terminology_schema"

-- 1) Target table (final shape)
CREATE TABLE IF NOT EXISTS :"terminology_schema".code_type (
  code_type varchar PRIMARY KEY
);

-- 2) OPTIONAL one-time backfill from legacy table if present
DO $$
DECLARE
  src_exists   boolean;
  has_code     boolean;
  has_code_typ boolean;
BEGIN
  SELECT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = :'terminology_schema' AND table_name = 'condition_code_type'
  ) INTO src_exists;

  IF src_exists THEN
    SELECT EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema = :'terminology_schema' AND table_name = 'condition_code_type' AND column_name = 'code'
    ) INTO has_code;

    SELECT EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema = :'terminology_schema' AND table_name = 'condition_code_type' AND column_name = 'code_type'
    ) INTO has_code_typ;

    IF has_code THEN
      EXECUTE format(
        'INSERT INTO %I.code_type (code_type)
         SELECT DISTINCT code FROM %I.condition_code_type
         WHERE code IS NOT NULL
         ON CONFLICT (code_type) DO NOTHING',
        :'terminology_schema', :'terminology_schema'
      );
    END IF;

    IF has_code_typ THEN
      EXECUTE format(
        'INSERT INTO %I.code_type (code_type)
         SELECT DISTINCT code_type FROM %I.condition_code_type
         WHERE code_type IS NOT NULL
         ON CONFLICT (code_type) DO NOTHING',
        :'terminology_schema', :'terminology_schema'
      );
    END IF;
  END IF;
END$$;

-- 3) Docs
COMMENT ON TABLE  :"terminology_schema".code_type IS 'Generic terminology: code types (single-column value set).';
COMMENT ON COLUMN :"terminology_schema".code_type.code_type IS 'The type of the code.';
