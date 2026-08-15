-- db/terminology/bill_type.sql
-- Terminology: Bill Type (code → description, deprecation metadata).
-- Uses psql var :"terminology_schema"

-- Create table if missing (with full target shape)
CREATE TABLE IF NOT EXISTS :"terminology_schema".bill_type (
  bill_type_code         varchar PRIMARY KEY,
  bill_type_description  varchar,
  deprecated             integer,   -- 0/1 flag
  deprecated_date        date,
  CONSTRAINT bill_type_deprecated_01 CHECK (deprecated IS NULL OR deprecated IN (0,1))
);

-- If the table existed previously with fewer/different columns, align it.
ALTER TABLE :"terminology_schema".bill_type
  ADD COLUMN IF NOT EXISTS bill_type_description varchar,
  ADD COLUMN IF NOT EXISTS deprecated            integer,
  ADD COLUMN IF NOT EXISTS deprecated_date       date;

-- Add the 0/1 CHECK constraint if it wasn't present before.
DO $do$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint c
    JOIN pg_class r ON r.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = r.relnamespace
    WHERE c.conname = 'bill_type_deprecated_01'
      AND r.relname = 'bill_type'
      AND n.nspname = current_schema()  -- search_path already set by wrapper
  ) THEN
    ALTER TABLE bill_type
      ADD CONSTRAINT bill_type_deprecated_01
      CHECK (deprecated IS NULL OR deprecated IN (0,1));
  END IF;
END
$do$;

-- Helpful comments
COMMENT ON TABLE  :"terminology_schema".bill_type IS 'Terminology: billing type codes and descriptions with deprecation metadata.';
COMMENT ON COLUMN :"terminology_schema".bill_type.bill_type_code        IS 'The code representing the type of bill.';
COMMENT ON COLUMN :"terminology_schema".bill_type.bill_type_description IS 'A description of the billing type.';
COMMENT ON COLUMN :"terminology_schema".bill_type.deprecated            IS '0/1 flag indicating if the billing type is deprecated.';
COMMENT ON COLUMN :"terminology_schema".bill_type.deprecated_date       IS 'Date when the billing type code was deprecated.';
