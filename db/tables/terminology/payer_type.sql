-- db/terminology/payer_type.sql
-- Terminology: Payer Type (single-column value set).
-- Uses psql var :"terminology_schema"

-- 1) Target shape
CREATE TABLE IF NOT EXISTS :"terminology_schema".payer_type (
  payer_type varchar PRIMARY KEY
);

-- 2) Align columns for legacy installs (safe to re-run)
ALTER TABLE :"terminology_schema".payer_type
  ADD COLUMN IF NOT EXISTS payer_type varchar;

-- 3) Docs
COMMENT ON TABLE  :"terminology_schema".payer_type
  IS 'Terminology: payer type values (e.g., Commercial, Medicare, Medicaid).';
COMMENT ON COLUMN :"terminology_schema".payer_type.payer_type
  IS 'The type of payer.';
