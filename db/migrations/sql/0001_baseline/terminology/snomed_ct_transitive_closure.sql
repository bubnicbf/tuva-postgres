-- db/terminology/snomed_ct_transitive_closure.sql
-- Terminology: SNOMED CT transitive closure (ancestor/descendant pairs)
-- Uses psql var :"terminology_schema"

CREATE TABLE IF NOT EXISTS :"terminology_schema".snomed_ct_transitive_closure (
  parent_snomed_code varchar NOT NULL,
  parent_description varchar,
  child_snomed_code  varchar NOT NULL,
  child_description  varchar,
  CONSTRAINT snomed_ct_tc_pk PRIMARY KEY (parent_snomed_code, child_snomed_code)
);

-- Helpful lookups
CREATE INDEX IF NOT EXISTS snomed_ct_tc_parent_idx ON :"terminology_schema".snomed_ct_transitive_closure (parent_snomed_code);
CREATE INDEX IF NOT EXISTS snomed_ct_tc_child_idx  ON :"terminology_schema".snomed_ct_transitive_closure (child_snomed_code);

-- Docs
COMMENT ON TABLE  :"terminology_schema".snomed_ct_transitive_closure IS
  'SNOMED CT transitive closure: parent→child (ancestor→descendant) relationships.';
COMMENT ON COLUMN :"terminology_schema".snomed_ct_transitive_closure.parent_snomed_code IS 'Parent SNOMED code.';
COMMENT ON COLUMN :"terminology_schema".snomed_ct_transitive_closure.parent_description IS 'Parent description.';
COMMENT ON COLUMN :"terminology_schema".snomed_ct_transitive_closure.child_snomed_code  IS 'Child SNOMED code.';
COMMENT ON COLUMN :"terminology_schema".snomed_ct_transitive_closure.child_description  IS 'Child description.';
