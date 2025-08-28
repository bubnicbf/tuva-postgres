-- db/tables/immunization__v_enriched.sql (patch)
CREATE OR REPLACE VIEW :"schema".v_immunization_enriched AS
SELECT
  i.*,
  sct.display  AS source_code_type_display,
  nct.display  AS normalized_code_type_display,
  cvx.long_description AS cvx_long_description
FROM :"schema".immunization i
LEFT JOIN :"terminology_schema".immunization_code_type sct
       ON i.source_code_type = sct.code
LEFT JOIN :"terminology_schema".immunization_code_type nct
       ON i.normalized_code_type = nct.code
LEFT JOIN :"terminology_schema".cvx cvx
       ON i.normalized_code_type = 'CVX'
      AND i.normalized_code      = cvx.cvx;
