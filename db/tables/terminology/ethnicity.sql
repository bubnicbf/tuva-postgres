-- db/terminology/ethnicity.sql
-- Uses psql var :"terminology_schema".

CREATE TABLE IF NOT EXISTS :"terminology_schema".ethnicity (
  code     varchar PRIMARY KEY,        -- e.g., 'hispanic', 'non_hispanic', 'unknown'
  display  varchar NOT NULL,
  system   varchar                     -- e.g., 'urn:us:omb:ethnicity'
);
