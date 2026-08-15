-- db/tables/eligibility__v_enriched.sql
-- Adds human-readable labels for coded fields.
-- Uses :"schema" and :"terminology_schema".

CREATE OR REPLACE VIEW :"schema".v_eligibility_enriched AS
SELECT
  e.*,
  pt.display  AS payer_type_display,
  orec.display AS original_reason_entitlement_display,
  dsc.display  AS dual_status_display,
  msc.display  AS medicare_status_display
FROM :"schema".eligibility e
LEFT JOIN :"terminology_schema".payer_type                       pt   ON e.payer_type = pt.code
LEFT JOIN :"terminology_schema".original_reason_entitlement_code orec ON e.original_reason_entitlement_code = orec.code
LEFT JOIN :"terminology_schema".dual_status_code                 dsc  ON e.dual_status_code = dsc.code
LEFT JOIN :"terminology_schema".medicare_status_code             msc  ON e.medicare_status_code = msc.code;
