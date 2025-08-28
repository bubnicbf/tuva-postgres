-- Uses :"schema", :"terminology_schema"
CREATE OR REPLACE VIEW :"schema".v_encounter_admit_enriched AS
SELECT
  e.*,
  at.admit_type_description AS admit_type_description_term,
  -- Flag if the in-row description disagrees with terminology (soft)
  CASE
    WHEN e.admit_type_description IS NULL OR at.admit_type_description IS NULL THEN FALSE
    ELSE (e.admit_type_description <> at.admit_type_description)
  END AS admit_type_desc_mismatch
FROM :"schema".encounter e
LEFT JOIN :"terminology_schema".admit_type at
  ON e.admit_type_code = at.admit_type_code;
