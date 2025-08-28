-- Small controlled list of code systems used for lab ORDERS.
-- Reuse for normalized/source order types.
CREATE TABLE IF NOT EXISTS :"terminology_schema".lab_order_code_type (
  code    varchar PRIMARY KEY,           -- e.g., 'LOINC','LOCAL','SNOMED'
  display varchar NOT NULL,
  system  varchar                        -- optional URI/URN
);
