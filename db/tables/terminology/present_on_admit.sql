-- POA indicator (UB-04). Common values include 'Y','N','U','W','1' (exempt), etc.
CREATE TABLE IF NOT EXISTS :"terminology_schema".present_on_admit (
  code    varchar PRIMARY KEY,
  display varchar NOT NULL,
  system  varchar
);
