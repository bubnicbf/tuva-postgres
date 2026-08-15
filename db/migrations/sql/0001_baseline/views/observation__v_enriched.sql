-- db/tables/observation__v_enriched.sql (patch)
-- Uses :"schema", :"terminology_schema"
CREATE OR REPLACE VIEW :"schema".v_observation_enriched AS
SELECT
  o.*,
  l.long_common_name AS loinc_long_common_name,
  l.short_name       AS loinc_short_name,
  l.class_code       AS loinc_class_code,
  l.class_description AS loinc_class_description,
  l.scale_type       AS loinc_scale_type
FROM :"schema".observation o
LEFT JOIN :"terminology_schema".loinc l
  ON o.normalized_code_type = 'LOINC'
 AND o.normalized_code      = l.loinc;
