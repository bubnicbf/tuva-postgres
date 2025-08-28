-- Uses :"terminology_schema"
CREATE TABLE IF NOT EXISTS :"terminology_schema".hcpcs_modifier (
  modifier     varchar PRIMARY KEY,  -- e.g., 25, 59, LT, RT
  description  varchar
);

-- Helpful index and docs
CREATE INDEX IF NOT EXISTS hcpcs_modifier_desc_idx
  ON :"terminology_schema".hcpcs_modifier (description);

COMMENT ON TABLE  :"terminology_schema".hcpcs_modifier
  IS 'HCPCS/CPT modifiers (two-character alphanumeric).';
COMMENT ON COLUMN :"terminology_schema".hcpcs_modifier.modifier
  IS 'Modifier code (typically 2 characters: A–Z or 0–9).';
