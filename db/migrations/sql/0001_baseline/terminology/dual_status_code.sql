CREATE TABLE IF NOT EXISTS :"terminology_schema".dual_status_code (
  code    varchar PRIMARY KEY,    -- e.g., '00','01','02','04','08','09'
  display varchar NOT NULL,       -- e.g., 'Not dual', 'QMB only', 'QMB+Medicaid', ...
  system  varchar
);
