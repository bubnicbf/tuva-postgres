-- db/terminology/ndc__v_normalized.sql
-- Convenience view exposing digits-only 11-char NDC for easy joins
CREATE OR REPLACE VIEW :"terminology_schema".v_ndc_normalized AS
SELECT
  ndc,
  REGEXP_REPLACE(ndc, '\D', '', 'g') AS ndc11,  -- canonical digits-only form
  rxcui,
  rxnorm_description,
  fda_description
FROM :"terminology_schema".ndc;
