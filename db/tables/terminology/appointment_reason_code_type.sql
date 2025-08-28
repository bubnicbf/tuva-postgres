-- Code-system identifiers for appointment reasons (diagnosis/problem catalogs).
CREATE TABLE IF NOT EXISTS :"terminology_schema".appointment_reason_code_type (
  code    varchar PRIMARY KEY,   -- e.g., 'ICD-10-CM','SNOMED','LOCAL'
  display varchar NOT NULL,
  system  varchar
);
