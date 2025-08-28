-- db/terminology/sex.sql
-- Small terminology set (seedable).
-- Uses psql var :"terminology_schema".

CREATE TABLE IF NOT EXISTS :"terminology_schema".sex (
  code     varchar PRIMARY KEY,        -- e.g., 'male', 'female', 'other', 'unknown'
  display  varchar NOT NULL,           -- human-readable label
  system   varchar DEFAULT 'http://hl7.org/fhir/administrative-gender'
);
