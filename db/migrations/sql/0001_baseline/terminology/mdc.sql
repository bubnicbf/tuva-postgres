-- db/terminology/mdc.sql
-- Terminology: Major Diagnostic Category (MDC)
-- Uses psql var :"terminology_schema"

-- 1) Target shape
CREATE TABLE IF NOT EXISTS :"terminology_schema".mdc (
  mdc_code        varchar PRIMARY KEY,
  mdc_description varchar
);

-- 2) Align columns for legacy installs (safe to re-run)
ALTER TABLE :"terminology_schema".mdc
  ADD COLUMN IF NOT EXISTS mdc_code        varchar,
  ADD COLUMN IF NOT EXISTS mdc_description varchar;

-- 3) Helpful lookup index (optional)
CREATE INDEX IF NOT EXISTS mdc_description_idx
  ON :"terminology_schema".mdc (mdc_description);

-- 4) Docs
COMMENT ON TABLE  :"terminology_schema".mdc IS
  'Terminology: Major Diagnostic Category (MDC) codes and descriptions.';
COMMENT ON COLUMN :"terminology_schema".mdc.mdc_code IS
  'The Major Diagnostic Category (MDC) code.';
COMMENT ON COLUMN :"terminology_schema".mdc.mdc_description IS
  'The description of the Major Diagnostic Category (MDC).';
