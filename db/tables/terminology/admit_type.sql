-- db/terminology/admit_type.sql
CREATE TABLE IF NOT EXISTS :"terminology_schema".admit_type (
  code     varchar PRIMARY KEY,     -- e.g., 'elective', 'urgent', 'emergency'
  display  varchar NOT NULL,
  system   varchar
);
