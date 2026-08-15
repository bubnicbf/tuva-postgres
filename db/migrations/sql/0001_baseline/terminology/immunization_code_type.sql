-- Normalizes code-system identifiers used in immunization.{source|normalized}_code_type.
CREATE TABLE IF NOT EXISTS :"terminology_schema".immunization_code_type (
  code    varchar PRIMARY KEY,   -- e.g., 'CVX','CPT','NDC','SNOMED','LOCAL'
  display varchar NOT NULL,
  system  varchar                -- optional URI/URN namespace
);
