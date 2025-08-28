CREATE TABLE IF NOT EXISTS :"terminology_schema".claim_type (
  code    varchar PRIMARY KEY,     -- e.g., 'professional', 'institutional', 'dental', 'vision'
  display varchar NOT NULL,
  system  varchar
);
