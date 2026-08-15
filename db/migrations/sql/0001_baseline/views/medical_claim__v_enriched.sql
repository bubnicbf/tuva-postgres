-- db/tables/medical_claim__v_enriched.sql
-- Adds human-readable labels for coded fields.
-- Uses :"schema" and :"terminology_schema".

CREATE OR REPLACE VIEW :"schema".v_medical_claim_enriched AS
SELECT
  m.*,
  ct.display  AS claim_type_display,
  pos.display AS place_of_service_display,
  bt.display  AS bill_type_display,
  rc.display  AS revenue_center_display,
  sc1.display AS service_category_1_display,
  sc2.display AS service_category_2_display,
  hc.description AS hcpcs_description,
  mod1.display AS hcpcs_modifier_1_display,
  mod2.display AS hcpcs_modifier_2_display,
  mod3.display AS hcpcs_modifier_3_display,
  mod4.display AS hcpcs_modifier_4_display,
  mod5.display AS hcpcs_modifier_5_display
FROM :"schema".medical_claim m
LEFT JOIN :"terminology_schema".claim_type        ct   ON m.claim_type = ct.code
LEFT JOIN :"terminology_schema".place_of_service  pos  ON m.place_of_service_code = pos.code
LEFT JOIN :"terminology_schema".bill_type         bt   ON m.bill_type_code = bt.code
LEFT JOIN :"terminology_schema".revenue_center    rc   ON m.revenue_center_code = rc.code
LEFT JOIN :"terminology_schema".service_category  sc1  ON sc1.level = 1 AND m.service_category_1 = sc1.code
LEFT JOIN :"terminology_schema".service_category  sc2  ON sc2.level = 2 AND m.service_category_2 = sc2.code
LEFT JOIN :"terminology_schema".hcpcs_code        hc   ON m.hcpcs_code = hc.code
LEFT JOIN :"terminology_schema".hcpcs_modifier    mod1 ON m.hcpcs_modifier_1 = mod1.code
LEFT JOIN :"terminology_schema".hcpcs_modifier    mod2 ON m.hcpcs_modifier_2 = mod2.code
LEFT JOIN :"terminology_schema".hcpcs_modifier    mod3 ON m.hcpcs_modifier_3 = mod3.code
LEFT JOIN :"terminology_schema".hcpcs_modifier    mod4 ON m.hcpcs_modifier_4 = mod4.code
LEFT JOIN :"terminology_schema".hcpcs_modifier    mod5 ON m.hcpcs_modifier_5 = mod5.code;
