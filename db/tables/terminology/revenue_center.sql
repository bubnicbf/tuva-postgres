CREATE TABLE IF NOT EXISTS :"terminology_schema".revenue_center (
  code    varchar PRIMARY KEY,     -- UB revenue code, e.g., '0450','0250'
  display varchar NOT NULL,
  system  varchar
);
