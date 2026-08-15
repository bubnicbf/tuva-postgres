-- db/terminology/admit_source.sql
-- Lookup for admission source codes and descriptions.
-- Uses psql var :"terminology_schema" supplied by the wrapper.

-- Create the table if it doesn't exist
CREATE TABLE IF NOT EXISTS :"terminology_schema".admit_source (
  admit_source_code         varchar PRIMARY KEY,   -- e.g., '1', '2', etc. (source-defined)
  admit_source_description  varchar                -- human-readable description
);

-- Add/align columns for the current schema version (safe to re-run)
ALTER TABLE :"terminology_schema".admit_source
  ADD COLUMN IF NOT EXISTS newborn_description varchar;  -- used when admit_type_code = '4' (Newborn)

-- Helpful comment for downstream users
COMMENT ON COLUMN :"terminology_schema".admit_source.newborn_description IS
  'Optional note/label for newborn admissions; referenced when admit_type_code = 4 (Newborn).';
