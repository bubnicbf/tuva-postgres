CREATE TABLE IF NOT EXISTS :"terminology_schema".original_reason_entitlement_code (
  code    varchar PRIMARY KEY,    -- e.g., '0','1','2','3','A','B', etc. (Medicare OREC)
  display varchar NOT NULL,
  system  varchar
);
