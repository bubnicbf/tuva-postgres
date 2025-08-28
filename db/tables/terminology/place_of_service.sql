-- db/terminology/place_of_service.sql
-- Terminology: Place of Service (code → description).
-- Uses psql var :"terminology_schema"

-- 1) Target shape
CREATE TABLE IF NOT EXISTS :"terminology_schema".place_of_service (
  place_of_service_code        varchar PRIMARY KEY,
  place_of_service_description varchar
);

-- 2) Align columns for legacy installs (safe to re-run)
ALTER TABLE :"terminology_schema".place_of_service
  ADD COLUMN IF NOT EXISTS place_of_service_code        varchar,
  ADD COLUMN IF NOT EXISTS place_of_service_description varchar;

-- 3) Helpful lookup index (optional)
CREATE INDEX IF NOT EXISTS place_of_service_desc_idx
  ON :"terminology_schema".place_of_service (place_of_service_description);

-- 4) Docs
COMMENT ON TABLE  :"terminology_schema".place_of_service
  IS 'Terminology: place of service codes and descriptions.';
COMMENT ON COLUMN :"terminology_schema".place_of_service.place_of_service_code
  IS 'The code representing the place of service.';
COMMENT ON COLUMN :"terminology_schema".place_of_service.place_of_service_description
  IS 'A description of the place of service.';
