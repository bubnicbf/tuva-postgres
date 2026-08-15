-- db/terminology/appointment_status.sql
-- Terminology: Appointment Status (code only).
-- Uses psql var :"terminology_schema"

CREATE TABLE IF NOT EXISTS :"terminology_schema".appointment_status (
  code  varchar PRIMARY KEY
);

-- Drop legacy columns if they exist (from older versions)
ALTER TABLE :"terminology_schema".appointment_status
  DROP COLUMN IF EXISTS display,
  DROP COLUMN IF EXISTS system;

COMMENT ON TABLE  :"terminology_schema".appointment_status IS 'Normalized set of appointment status codes (code only).';
COMMENT ON COLUMN :"terminology_schema".appointment_status.code  IS 'The code for the appointment status.';
