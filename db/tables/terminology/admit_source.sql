-- db/terminology/admit_source.sql
CREATE TABLE IF NOT EXISTS :"terminology_schema".admit_source (
  code     varchar PRIMARY KEY,     -- e.g., 'ER', 'clinic', 'transfer', ...
  display  varchar NOT NULL,
  system   varchar
);
