-- Uses :"schema", :"terminology_schema"
CREATE OR REPLACE VIEW :"schema".v_encounter_admit_enriched AS
SELECT
  e.*,
  a.admit_source_description AS admit_source_description_term,
  CASE
    WHEN e.admit_type_code IN ('4','04') THEN a.newborn_description
    ELSE NULL
  END AS admit_source_newborn_description
FROM :"schema".encounter e
LEFT JOIN :"terminology_schema".admit_source a
  ON e.admit_source_code = a.admit_source_code;
