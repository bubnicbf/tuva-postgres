-- db/tables/procedure__v_enriched.sql
-- Adds friendly labels for code types, codes, and modifiers.
-- Uses :"schema" and :"terminology_schema".

CREATE OR REPLACE VIEW :"schema".v_procedure_enriched AS
SELECT
  pr.*,
  sct.display AS source_code_type_display,
  nct.display AS normalized_code_type_display,
  sc.description AS source_code_description_term,
  nc.description AS normalized_code_description_term,
  m1.display AS modifier_1_display,
  m2.display AS modifier_2_display,
  m3.display AS modifier_3_display,
  m4.display AS modifier_4_display,
  m5.display AS modifier_5_display
FROM :"schema".procedure pr
LEFT JOIN :"terminology_schema".procedure_code_type sct
       ON pr.source_code_type = sct.code
LEFT JOIN :"terminology_schema".procedure_code_type nct
       ON pr.normalized_code_type = nct.code
LEFT JOIN :"terminology_schema".procedure_code sc
       ON pr.source_code_type = sc.code_type AND pr.source_code = sc.code
LEFT JOIN :"terminology_schema".procedure_code nc
       ON pr.normalized_code_type = nc.code_type AND pr.normalized_code = nc.code
LEFT JOIN :"terminology_schema".hcpcs_modifier m1 ON pr.modifier_1 = m1.code
LEFT JOIN :"terminology_schema".hcpcs_modifier m2 ON pr.modifier_2 = m2.code
LEFT JOIN :"terminology_schema".hcpcs_modifier m3 ON pr.modifier_3 = m3.code
LEFT JOIN :"terminology_schema".hcpcs_modifier m4 ON pr.modifier_4 = m4.code
LEFT JOIN :"terminology_schema".hcpcs_modifier m5 ON pr.modifier_5 = m5.code;
