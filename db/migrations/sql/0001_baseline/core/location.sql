-- db/tables/location.sql
-- Core model: one record per physical location/facility.
-- Uses psql var :"schema" supplied by the wrapper.

CREATE TABLE IF NOT EXISTS :"schema".location (
  location_id     varchar PRIMARY KEY,

  npi             varchar,     -- facility/organization NPI (directory-size; adapter-hydrated)
  name            varchar,
  facility_type   varchar,
  parent_organization varchar,

  address         varchar,
  city            varchar,
  state           varchar,
  zip_code        varchar,

  latitude        double precision,
  longitude       double precision,

  data_source     varchar,
  tuva_last_run   timestamp without time zone,

  -- Pragmatic DQ guardrails (kept simple to avoid load friction)
  -- ZIP: allow 5 or 9 digits (hyphen optional)
  CONSTRAINT loc_zip_format CHECK (
    zip_code IS NULL
    OR zip_code ~ '^\d{5}(-?\d{4})?$'
  ),
  -- State: 2 letters (if provided)
  CONSTRAINT loc_state_2char CHECK (
    state IS NULL
    OR state ~ '^[A-Za-z]{2}$'
  ),
  -- Lat/Lon ranges
  CONSTRAINT loc_lat_range CHECK (
    latitude IS NULL
    OR (latitude >= -90 AND latitude <= 90)
  ),
  CONSTRAINT loc_lon_range CHECK (
    longitude IS NULL
    OR (longitude >= -180 AND longitude <= 180)
  ),
  -- NPI basic plausibility: 10 digits (full Luhn check is in tests)
  CONSTRAINT loc_npi_10_digits CHECK (
    npi IS NULL
    OR npi ~ '^\d{10}$'
  ),
  -- tuva_last_run cannot be in the future
  CONSTRAINT loc_tuva_last_run_not_future CHECK (
    tuva_last_run IS NULL
    OR tuva_last_run <= (NOW()::timestamp without time zone)
  )
);

-- Helpful indexes
CREATE INDEX IF NOT EXISTS loc_npi_idx        ON :"schema".location (npi);
CREATE INDEX IF NOT EXISTS loc_name_idx       ON :"schema".location (name);
CREATE INDEX IF NOT EXISTS loc_city_state_idx ON :"schema".location (state, city);
CREATE INDEX IF NOT EXISTS loc_zip_idx        ON :"schema".location (zip_code);
CREATE INDEX IF NOT EXISTS loc_facility_idx   ON :"schema".location (facility_type);
CREATE INDEX IF NOT EXISTS loc_parent_org_idx ON :"schema".location (parent_organization);
