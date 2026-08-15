-- Large dictionary of procedure codes across systems (adapter-hydrated; do NOT seed)
CREATE TABLE IF NOT EXISTS :"terminology_schema".procedure_code (
  code_type            varchar NOT NULL,     -- FK -> procedure_code_type.code (soft)
  code                 varchar NOT NULL,
  description          varchar,
  effective_start_date date,
  effective_end_date   date,
  terminology_source   varchar,              -- e.g., 'AMA_CPT','CMS_HCPCS','ICD10PCS','LOINC'
  terminology_version  varchar,
  PRIMARY KEY (code_type, code)
);

CREATE INDEX IF NOT EXISTS procedure_code_desc_idx
  ON :"terminology_schema".procedure_code (code_type, description);
