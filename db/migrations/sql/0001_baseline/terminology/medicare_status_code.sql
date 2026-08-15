CREATE TABLE IF NOT EXISTS :"terminology_schema".medicare_status_code (
  code    varchar PRIMARY KEY,    -- e.g., 'A','B','C','D','E','M','T' (plan/status flavors vary by source)
  display varchar NOT NULL,
  system  varchar
);
