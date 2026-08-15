-- db/terminology/appointment_cancellation_reason_code_type.sql
-- Defines the Appointment Cancellation Reason lookup (code → description).
-- Uses psql var :"terminology_schema" supplied by the wrapper.

-- New canonical table (simple value set)
CREATE TABLE IF NOT EXISTS :"terminology_schema".appointment_cancellation_reason (
  code        varchar PRIMARY KEY,
  description varchar
);

-- INSERT INTO :"terminology_schema".appointment_cancellation_reason (code, description)
-- SELECT code, display
-- FROM   :"terminology_schema".appointment_cancellation_reason_code_type
-- ON CONFLICT (code) DO UPDATE SET description = EXCLUDED.description;

-- Optional docs
COMMENT ON TABLE  :"terminology_schema".appointment_cancellation_reason
  IS 'Terminology: appointment cancellation reasons (normalized value set).';
COMMENT ON COLUMN :"terminology_schema".appointment_cancellation_reason.code
  IS 'The code for the cancellation reason.';
COMMENT ON COLUMN :"terminology_schema".appointment_cancellation_reason.description
  IS 'The description of the cancellation reason code.';
