-- Terminology: Medicare Original Reason for Entitlement (OREC)
-- Uses psql var :"terminology_schema"

CREATE TABLE IF NOT EXISTS :"terminology_schema".medicare_orec (
  original_reason_entitlement_code        varchar PRIMARY KEY,
  original_reason_entitlement_description varchar
);

ALTER TABLE :"terminology_schema".medicare_orec
  ADD COLUMN IF NOT EXISTS original_reason_entitlement_code        varchar,
  ADD COLUMN IF NOT EXISTS original_reason_entitlement_description varchar;

CREATE INDEX IF NOT EXISTS medicare_orec_desc_idx
  ON :"terminology_schema".medicare_orec (original_reason_entitlement_description);

COMMENT ON TABLE  :"terminology_schema".medicare_orec
  IS 'Terminology: Medicare Original Reason for Entitlement (OREC) codes.';
COMMENT ON COLUMN :"terminology_schema".medicare_orec.original_reason_entitlement_code
  IS 'The code representing the original reason for Medicare entitlement.';
COMMENT ON COLUMN :"terminology_schema".medicare_orec.original_reason_entitlement_description
  IS 'A description of the original reason for Medicare entitlement.';
