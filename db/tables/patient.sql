-- db/tables/patient.sql
-- Core model: one record per person/patient.
-- Uses psql var :"schema" supplied by the wrapper.

CREATE TABLE IF NOT EXISTS :"schema".patient (
  person_id               varchar PRIMARY KEY,
  name_suffix             varchar,
  first_name              varchar,
  middle_name             varchar,
  last_name               varchar,

  -- Coded fields (joinable to terminology)
  sex                     varchar,
  race                    varchar,
  ethnicity               varchar,

  birth_date              date,
  death_date              date,
  death_flag              integer,                       -- 0/1 flag

  social_security_number  varchar,
  address                 varchar,
  city                    varchar,
  state                   varchar,
  zip_code                varchar,
  county                  varchar,
  latitude                double precision,              -- Postgres float8
  longitude               double precision,              -- Postgres float8
  phone                   varchar,
  email                   varchar,

  data_source             varchar,
  age                     integer,                       -- computed at build time
  age_group               varchar,                       -- e.g., '20-29', '30-39'
  tuva_last_run           timestamp without time zone,   -- "timestamp_ntz"

  -- Basic data quality guards
  CONSTRAINT death_flag_bool CHECK (death_flag IN (0,1)),
  CONSTRAINT lat_range CHECK (latitude  IS NULL OR (latitude  BETWEEN -90  AND 90)),
  CONSTRAINT lon_range CHECK (longitude IS NULL OR (longitude BETWEEN -180 AND 180)),
  CONSTRAINT death_after_birth CHECK (
    death_date IS NULL OR birth_date IS NULL OR death_date >= birth_date
  )
);

-- Helpful indexes for common access patterns
CREATE INDEX IF NOT EXISTS patient_zip_idx         ON :"schema".patient (zip_code);
CREATE INDEX IF NOT EXISTS patient_state_city_idx  ON :"schema".patient (state, city);
CREATE INDEX IF NOT EXISTS patient_last_first_idx  ON :"schema".patient (last_name, first_name);
CREATE INDEX IF NOT EXISTS patient_birth_date_idx  ON :"schema".patient (birth_date);
CREATE INDEX IF NOT EXISTS patient_sex_idx         ON :"schema".patient (sex);
CREATE INDEX IF NOT EXISTS patient_race_idx        ON :"schema".patient (race);
CREATE INDEX IF NOT EXISTS patient_ethnicity_idx   ON :"schema".patient (ethnicity);
