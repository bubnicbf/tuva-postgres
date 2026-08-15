-- db/tables/practitioner__v_enriched.sql
-- Convenience view: core practitioner enriched by NPI terminology.
-- Uses :"schema" and :"terminology_schema".

CREATE OR REPLACE VIEW :"schema".v_practitioner_enriched AS
SELECT
  p.*,
  t.entity_type_code,
  t.primary_taxonomy_code,
  t.primary_specialty_description AS terminology_specialty,
  t.practice_city   AS terminology_practice_city,
  t.practice_state  AS terminology_practice_state,
  t.practice_zip_code AS terminology_practice_zip,
  t.last_updated    AS terminology_last_updated
FROM :"schema".practitioner p
LEFT JOIN :"terminology_schema".provider_npi t
  ON p.npi = t.npi;
