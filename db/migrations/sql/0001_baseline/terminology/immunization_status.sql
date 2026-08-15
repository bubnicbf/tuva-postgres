-- Terminology: Immunization Status (code → display)
-- Uses psql var :"terminology_schema"

CREATE TABLE IF NOT EXISTS :"terminology_schema".immunization_status (
  status_code varchar PRIMARY KEY,
  status      varchar
);

ALTER TABLE :"terminology_schema".immunization_status
  ADD COLUMN IF NOT EXISTS status_code varchar,
  ADD COLUMN IF NOT EXISTS status      varchar;

CREATE INDEX IF NOT EXISTS immunization_status_idx
  ON :"terminology_schema".immunization_status (status);

COMMENT ON TABLE  :"terminology_schema".immunization_status IS
  'Immunization status codes and display text.';
COMMENT ON COLUMN :"terminology_schema".immunization_status.status_code IS
  'The code representing the immunization status.';
COMMENT ON COLUMN :"terminology_schema".immunization_status.status IS
  'The display of the immunization status.';
