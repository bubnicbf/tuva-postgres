-- db/terminology/admit_type.sql
-- Lookup for admission type codes and descriptions (e.g., UB-04 admit types).
-- Uses psql var :"terminology_schema" supplied by the wrapper.

-- Create table if missing
CREATE TABLE IF NOT EXISTS :"terminology_schema".admit_type (
  admit_type_code         varchar PRIMARY KEY,
  admit_type_description  varchar
);

-- Ensure required columns exist (safe to re-run)
ALTER TABLE :"terminology_schema".admit_type
  ADD COLUMN IF NOT EXISTS admit_type_description varchar;

-- Helpful comments
COMMENT ON TABLE  :"terminology_schema".admit_type IS 'Terminology: admission types (code → description).';
COMMENT ON COLUMN :"terminology_schema".admit_type.admit_type_code IS 'Code representing the type of admission.';
COMMENT ON COLUMN :"terminology_schema".admit_type.admit_type_description IS 'Human-readable description of the admission type.';
