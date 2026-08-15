-- Uses :"schema", :"terminology_schema"
CREATE OR REPLACE VIEW :"schema".v_medical_claim_admit_enriched AS
SELECT
  m.*,
  at.admit_type_description AS admit_type_description_term,
  CASE
    WHEN m.admit_type_description IS NULL OR at.admit_type_description IS NULL THEN FALSE
    ELSE (m.admit_type_description <> at.admit_type_description)
  END AS admit_type_desc_mismatch
FROM :"schema".medical_claim m
LEFT JOIN :"terminology_schema".admit_type at
  ON m.admit_type_code = at.admit_type_code;
