-- Small controlled list of code systems used in observations.
-- Uses psql var :"terminology_schema".
CREATE TABLE IF NOT EXISTS :"terminology_schema".observation_code_type (
  code    varchar PRIMARY KEY,     -- e.g., 'LOINC','SNOMED','LOCAL','ICD-10','NDC'
  display varchar NOT NULL,
  system  varchar                  -- optional URI/URN namespace
);
