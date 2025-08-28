-- db/terminology/discharge_disposition.sql
CREATE TABLE IF NOT EXISTS :"terminology_schema".discharge_disposition (
  code     varchar PRIMARY KEY,     -- e.g., 'home', 'skilled_nursing', 'expired'
  display  varchar NOT NULL,
  system   varchar
);
