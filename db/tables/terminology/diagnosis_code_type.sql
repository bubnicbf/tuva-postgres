-- db/terminology/diagnosis_code_type.sql
CREATE TABLE IF NOT EXISTS :"terminology_schema".diagnosis_code_type (
  code     varchar PRIMARY KEY,     -- e.g., 'ICD-10-CM', 'ICD-9-CM', 'SNOMED-CT'
  display  varchar NOT NULL,
  system   varchar
);
