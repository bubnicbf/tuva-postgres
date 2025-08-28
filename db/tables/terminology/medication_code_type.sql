-- Normalizes code-system identifiers used in medication.source_code_type.
-- Examples cover common medication/vaccine catalogs and local codes.
CREATE TABLE IF NOT EXISTS :"terminology_schema".medication_code_type (
  code    varchar PRIMARY KEY,   -- e.g., 'NDC','RxNorm','ATC','CVX','LOCAL'
  display varchar NOT NULL,
  system  varchar                -- optional URI/URN
);
