CREATE TABLE IF NOT EXISTS :"terminology_schema".payer_type (
  code    varchar PRIMARY KEY,    -- e.g., 'commercial','medicare','medicaid','exchange','tricare','va','other'
  display varchar NOT NULL,
  system  varchar
);
