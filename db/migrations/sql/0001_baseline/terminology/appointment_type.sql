-- db/terminology/appointment_type.sql
-- Terminology: Appointment Type (code → description).
-- Uses psql var :"terminology_schema"

CREATE TABLE IF NOT EXISTS :"terminology_schema".appointment_type (
  code        varchar PRIMARY KEY,
  description varchar
);

-- OPTIONAL one-time backfill from legacy table if it exists
-- (safe to leave in; it will no-op if the old table is absent)
INSERT INTO :"terminology_schema".appointment_type (code, description)
SELECT code, display
FROM   :"terminology_schema".appointment_type_code
ON CONFLICT (code) DO UPDATE
SET description = EXCLUDED.description;

COMMENT ON TABLE  :"terminology_schema".appointment_type IS 'Normalized set of appointment type codes (code → description).';
COMMENT ON COLUMN :"terminology_schema".appointment_type.code        IS 'The code for the appointment type.';
COMMENT ON COLUMN :"terminology_schema".appointment_type.description IS 'The description of the appointment type code.';
