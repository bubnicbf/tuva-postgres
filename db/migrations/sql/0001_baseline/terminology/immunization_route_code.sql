-- Terminology: Immunization Route Code (code → description)
-- Uses psql var :"terminology_schema"

CREATE TABLE IF NOT EXISTS :"terminology_schema".immunization_route_code (
  route_code  varchar PRIMARY KEY,
  description varchar
);

ALTER TABLE :"terminology_schema".immunization_route_code
  ADD COLUMN IF NOT EXISTS route_code  varchar,
  ADD COLUMN IF NOT EXISTS description varchar;

CREATE INDEX IF NOT EXISTS immunization_route_code_desc_idx
  ON :"terminology_schema".immunization_route_code (description);

COMMENT ON TABLE  :"terminology_schema".immunization_route_code IS
  'Immunization route of administration codes.';
COMMENT ON COLUMN :"terminology_schema".immunization_route_code.route_code IS
  'The code representing the route of administration for the immunization.';
COMMENT ON COLUMN :"terminology_schema".immunization_route_code.description IS
  'A description of the route of administration for the immunization.';
