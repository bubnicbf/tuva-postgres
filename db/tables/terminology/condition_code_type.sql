-- Small controlled list of code systems for conditions/diagnoses.
-- Examples: 'ICD-10-CM','ICD-9-CM','SNOMED','LOCAL'
CREATE TABLE IF NOT EXISTS :"terminology_schema".condition_code_type (
  code    varchar PRIMARY KEY,
  display varchar NOT NULL,
  system  varchar
);
