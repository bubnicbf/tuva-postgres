-- db/terminology/ndc.sql
-- Terminology: NDC → RxNorm (RXCUI) + FDA description
-- Uses psql var :"terminology_schema"

-- 1) Target table
CREATE TABLE IF NOT EXISTS :"terminology_schema".ndc (
  ndc                 varchar PRIMARY KEY,   -- keep original formatting (may include dashes/spaces)
  rxcui               varchar,               -- RxNorm concept id
  rxnorm_description  varchar,               -- RxNorm concept name/desc
  fda_description     varchar                -- FDA SPL label/product desc
);

-- 2) Align columns for legacy installs (safe to re-run)
ALTER TABLE :"terminology_schema".ndc
  ADD COLUMN IF NOT EXISTS ndc                varchar,
  ADD COLUMN IF NOT EXISTS rxcui              varchar,
  ADD COLUMN IF NOT EXISTS rxnorm_description varchar,
  ADD COLUMN IF NOT EXISTS fda_description    varchar;

-- 3) Helpful indexes
-- Lookup by RXCUI
CREATE INDEX IF NOT EXISTS ndc_rxcui_idx
  ON :"terminology_schema".ndc (rxcui);

-- Normalized 11-digit (digits-only) lookup to match various NDC print formats
CREATE INDEX IF NOT EXISTS ndc_digits_idx
  ON :"terminology_schema".ndc (REGEXP_REPLACE(ndc, '\D', '', 'g'));

-- 4) Docs
COMMENT ON TABLE  :"terminology_schema".ndc IS
  'NDC terminology mapping: NDC → RxNorm (RXCUI, description) and FDA description.';
COMMENT ON COLUMN :"terminology_schema".ndc.ndc                IS 'The National Drug Code (original formatting retained).';
COMMENT ON COLUMN :"terminology_schema".ndc.rxcui              IS 'RxNorm RxCUI mapped to this NDC.';
COMMENT ON COLUMN :"terminology_schema".ndc.rxnorm_description IS 'Human-readable RxNorm concept description.';
COMMENT ON COLUMN :"terminology_schema".ndc.fda_description    IS 'FDA SPL label/product description for this NDC.';
