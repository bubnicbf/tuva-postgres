-- Terminology: Immunization Status Reason (reason_code + code_type → description)
-- Uses psql var :"terminology_schema"

CREATE TABLE IF NOT EXISTS :"terminology_schema".immunization_status_reason (
  reason_code  varchar NOT NULL,
  code_type    varchar,   -- optional (e.g., SNOMED, ICD-10, etc.)
  description  varchar,
  CONSTRAINT immunization_status_reason_pk PRIMARY KEY (reason_code)
);

ALTER TABLE :"terminology_schema".immunization_status_reason
  ADD COLUMN IF NOT EXISTS reason_code varchar,
  ADD COLUMN IF NOT EXISTS code_type   varchar,
  ADD COLUMN IF NOT EXISTS description varchar;

-- (Optional) If you maintain terminology.code_type, you can enforce membership later:
-- ALTER TABLE :"terminology_schema".immunization_status_reason
--   ADD CONSTRAINT immunization_status_reason_code_type_fk
--   FOREIGN KEY (code_type) REFERENCES :"terminology_schema".code_type(code_type)
--   DEFERRABLE INITIALLY DEFERRED NOT VALID;

CREATE INDEX IF NOT EXISTS immunization_status_reason_type_idx
  ON :"terminology_schema".immunization_status_reason (code_type);

COMMENT ON TABLE  :"terminology_schema".immunization_status_reason IS
  'Reasons for immunization status (may reference a code system via code_type).';
COMMENT ON COLUMN :"terminology_schema".immunization_status_reason.reason_code IS
  'The code representing the immunization status reason.';
COMMENT ON COLUMN :"terminology_schema".immunization_status_reason.code_type IS
  'The type of the code representing the immunization status reason.';
COMMENT ON COLUMN :"terminology_schema".immunization_status_reason.description IS
  'A description of the immunization status reason.';
