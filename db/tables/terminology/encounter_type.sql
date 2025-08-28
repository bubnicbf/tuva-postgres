-- db/terminology/encounter_type.sql
-- Uses psql var :"terminology_schema".

CREATE TABLE IF NOT EXISTS :"terminology_schema".encounter_type (
  code     varchar PRIMARY KEY,     -- e.g., 'acute_inpatient', 'ed', 'observation', ...
  display  varchar NOT NULL,
  system   varchar                  -- optional uri/urn
);
