-- Uses :"schema", :"terminology_schema"
CREATE OR REPLACE VIEW :"schema".v_medical_claim_billtype_enriched AS
SELECT
  m.*,
  bt.bill_type_description AS bill_type_description_term,
  bt.deprecated            AS bill_type_deprecated,
  bt.deprecated_date       AS bill_type_deprecated_date,
  -- Soft mismatch indicator (if you also store a description in medical_claim)
  CASE
    WHEN m.bill_type_description IS NULL OR bt.bill_type_description IS NULL THEN FALSE
    ELSE (m.bill_type_description <> bt.bill_type_description)
  END AS bill_type_desc_mismatch
FROM :"schema".medical_claim m
LEFT JOIN :"terminology_schema".bill_type bt
  ON m.bill_type_code = bt.bill_type_code;
