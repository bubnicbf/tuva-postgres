-- db/tables/pharmacy_claim__v_enriched.sql
-- Adds NDC attributes via terminology. Provider directories are adapter-fed, so we rely on names provided.
-- Uses :"schema" and :"terminology_schema".
CREATE OR REPLACE VIEW :"schema".v_pharmacy_claim_enriched AS
SELECT
  r.*,
  n.description   AS ndc_description_term,
  n.generic_name  AS ndc_generic_name,
  n.brand_name    AS ndc_brand_name,
  n.dosage_form   AS ndc_dosage_form,
  n.route         AS ndc_route
FROM :"schema".pharmacy_claim r
LEFT JOIN :"terminology_schema".ndc n
       ON REGEXP_REPLACE(r.ndc_code, '\D', '', 'g') = n.ndc11;  -- normalize for join
