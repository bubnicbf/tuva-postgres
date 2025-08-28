-- db/tables/condition__v_enriched.sql (replace file)
-- Uses :"schema", :"terminology_schema"
CREATE OR REPLACE VIEW :"schema".v_condition_enriched AS
SELECT
  c.*,
  -- new simple booleans since code_type is single-column
  (sct.code_type IS NOT NULL) AS source_code_type_known,
  (nct.code_type IS NOT NULL) AS normalized_code_type_known,
  poa.display                 AS present_on_admit_display
FROM :"schema".condition c
LEFT JOIN :"terminology_schema".code_type sct
       ON c.source_code_type = sct.code_type
LEFT JOIN :"terminology_schema".code_type nct
       ON c.normalized_code_type = nct.code_type
LEFT JOIN :"terminology_schema".present_on_admit poa
       ON c.present_on_admit_code = poa.code;
