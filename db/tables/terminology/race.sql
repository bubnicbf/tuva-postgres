-- db/terminology/race.sql
-- Uses psql var :"terminology_schema".
-- Values can follow OMB or internal codes; keep flexible.

CREATE TABLE IF NOT EXISTS :"terminology_schema".race (
  code     varchar PRIMARY KEY,        -- e.g., 'white', 'black', 'asian', 'ai_an', 'nh_pi', 'other', 'unknown'
  display  varchar NOT NULL,
  system   varchar                     -- e.g., 'urn:us:omb:race', 'http://hl7.org/fhir/us/core/ValueSet/omb-race-category'
);
