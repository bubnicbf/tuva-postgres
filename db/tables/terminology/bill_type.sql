CREATE TABLE IF NOT EXISTS :"terminology_schema".bill_type (
  code    varchar PRIMARY KEY,     -- UB-04 bill type, e.g., '111','131','851'
  display varchar NOT NULL,
  system  varchar
);
