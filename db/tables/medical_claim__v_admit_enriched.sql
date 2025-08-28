-- Uses :"schema", :"terminology_schema"
CREATE OR REPLACE VIEW :"schema".v_medical_claim_admit_enriched AS
SELECT
  m.*,
  a.admit_source_description AS admit_source_description_term,
  CASE
    WHEN m.admit_type_code IN ('4','04') THEN a.newborn_description
    ELSE NULL
  END AS admit_source_newborn_description
FROM :"schema".medical_claim m
LEFT JOIN :"terminology_schema".admit_source a
  ON m.admit_source_code = a.admit_source_code;
