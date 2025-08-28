-- Normalized appointment statuses (loosely aligned with HL7 FHIR).
CREATE TABLE IF NOT EXISTS :"terminology_schema".appointment_status (
  code    varchar PRIMARY KEY,   -- e.g., 'proposed','pending','booked','arrived','fulfilled','cancelled','noshow','entered-in-error'
  display varchar NOT NULL,
  system  varchar
);
