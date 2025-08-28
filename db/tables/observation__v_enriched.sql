-- db/tables/observation__v_enriched.sql
-- Adds a friendly label for source_code_type.
-- Uses :"schema" and :"terminology_schema".

CREATE OR REPLACE VIEW :"schema".v_observation_enriched AS
SELECT
  o.*,
  oct.display AS source_code_type_display
FROM :"schema".observation o
LEFT JOIN :"terminology_schema".observation_code_type oct
  ON o.source_code_type = oct.code;
