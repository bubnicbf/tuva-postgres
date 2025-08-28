-- Coded list of procedure code systems (small, seedable)
CREATE TABLE IF NOT EXISTS :"terminology_schema".procedure_code_type (
  code    varchar PRIMARY KEY,           -- e.g., 'CPT', 'HCPCS', 'ICD-10-PCS', 'LOINC', 'SNOMED', 'NDC', 'LOCAL'
  display varchar NOT NULL,
  system  varchar
);
