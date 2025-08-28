-- Code-system identifiers for cancellation reasons.
CREATE TABLE IF NOT EXISTS :"terminology_schema".appointment_cancellation_reason_code_type (
  code    varchar PRIMARY KEY,   -- e.g., 'SNOMED','LOCAL','ADMIN'
  display varchar NOT NULL,
  system  varchar
);
