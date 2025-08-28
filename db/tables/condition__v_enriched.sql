-- db/tables/condition__v_enriched.sql
-- Adds friendly labels for code types and POA.
-- Uses :"schema" and :"terminology_schema".

CREATE OR REPLACE VIEW :"schema".v_condition_enriched AS
SELECT
  c.*,
  sct.display  AS source_code_type_display,
  nct.display  AS normalized_code_type_display,
  poa.display  AS present_on_admit_display
FROM :"schema".condition c
LEFT JOIN :"terminology_schema".condition_code_type sct
       ON c.source_code_type = sct.code
LEFT JOIN :"terminology_schema".condition_code_type nct
       ON c.normalized_code_type = nct.code
LEFT JOIN :"terminology_schema".present_on_admit poa
       ON c.present_on_admit_code = poa.code;
