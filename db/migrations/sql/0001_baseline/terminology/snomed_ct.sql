-- db/terminology/snomed_ct.sql
-- Terminology: SNOMED CT concepts
-- Uses psql var :"terminology_schema"

CREATE TABLE IF NOT EXISTS :"terminology_schema".snomed_ct (
  snomed_ct    varchar PRIMARY KEY,     -- conceptId
  description  varchar,                 -- FSN or preferred term
  is_active    varchar,                 -- '1' active, '0' inactive
  created      date,
  last_updated date,
  CONSTRAINT snomed_ct_active_01 CHECK (is_active IS NULL OR is_active IN ('0','1'))
);

-- Helpful lookups
CREATE INDEX IF NOT EXISTS snomed_ct_desc_idx    ON :"terminology_schema".snomed_ct (description);
CREATE INDEX IF NOT EXISTS snomed_ct_active_idx  ON :"terminology_schema".snomed_ct (is_active);

-- Docs
COMMENT ON TABLE  :"terminology_schema".snomed_ct IS 'SNOMED CT concepts.';
COMMENT ON COLUMN :"terminology_schema".snomed_ct.snomed_ct    IS 'The SNOMED CT code.';
COMMENT ON COLUMN :"terminology_schema".snomed_ct.description  IS 'The description of the SNOMED CT code.';
COMMENT ON COLUMN :"terminology_schema".snomed_ct.is_active    IS '1 active, 0 inactive.';
COMMENT ON COLUMN :"terminology_schema".snomed_ct.created      IS 'Creation date.';
COMMENT ON COLUMN :"terminology_schema".snomed_ct.last_updated IS 'Last update date.';
