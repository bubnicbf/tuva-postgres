-- Terminology: Medicare Status (code → description)
-- Uses psql var :"terminology_schema"

CREATE TABLE IF NOT EXISTS :"terminology_schema".medicare_status (
  medicare_status_code        varchar PRIMARY KEY,
  medicare_status_description varchar
);

ALTER TABLE :"terminology_schema".medicare_status
  ADD COLUMN IF NOT EXISTS medicare_status_code        varchar,
  ADD COLUMN IF NOT EXISTS medicare_status_description varchar;

CREATE INDEX IF NOT EXISTS medicare_status_desc_idx
  ON :"terminology_schema".medicare_status (medicare_status_description);

COMMENT ON TABLE  :"terminology_schema".medicare_status
  IS 'Terminology: Medicare status codes.';
COMMENT ON COLUMN :"terminology_schema".medicare_status.medicare_status_code
  IS 'The code representing the Medicare status.';
COMMENT ON COLUMN :"terminology_schema".medicare_status.medicare_status_description
  IS 'A description of the Medicare status.';
