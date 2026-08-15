-- db/terminology/ms_drg.sql
-- Terminology: Medicare Severity DRG (code → MDC, med/surg, description, deprecation)
-- Uses psql var :"terminology_schema"

CREATE TABLE IF NOT EXISTS :"terminology_schema".ms_drg (
  ms_drg_code         varchar PRIMARY KEY,
  mdc_code            varchar,
  medical_surgical    varchar,     -- e.g., 'Medical' / 'Surgical'
  ms_drg_description  varchar,
  deprecated          integer,     -- 1 = deprecated, 0 = active
  deprecated_date     date,
  CONSTRAINT msdrg_deprecated_01 CHECK (deprecated IS NULL OR deprecated IN (0,1))
);

-- Helpful lookups
CREATE INDEX IF NOT EXISTS ms_drg_mdc_idx        ON :"terminology_schema".ms_drg (mdc_code);
CREATE INDEX IF NOT EXISTS ms_drg_med_surg_idx   ON :"terminology_schema".ms_drg (medical_surgical);
CREATE INDEX IF NOT EXISTS ms_drg_deprecated_idx ON :"terminology_schema".ms_drg (deprecated);

-- Docs
COMMENT ON TABLE  :"terminology_schema".ms_drg IS
  'MS-DRG: code → MDC, medical/surgical flag, description, deprecation signal.';
COMMENT ON COLUMN :"terminology_schema".ms_drg.ms_drg_code        IS 'The Medicare Severity Diagnosis Related Group (MS-DRG) code.';
COMMENT ON COLUMN :"terminology_schema".ms_drg.mdc_code           IS 'The Major Diagnostic Category (MDC) code associated with the MS-DRG.';
COMMENT ON COLUMN :"terminology_schema".ms_drg.medical_surgical   IS 'Indicates whether the DRG is medical or surgical.';
COMMENT ON COLUMN :"terminology_schema".ms_drg.ms_drg_description IS 'The description of the MS-DRG.';
COMMENT ON COLUMN :"terminology_schema".ms_drg.deprecated         IS '1 if the MS-DRG code is deprecated; else 0.';
COMMENT ON COLUMN :"terminology_schema".ms_drg.deprecated_date    IS 'Date the MS-DRG code was deprecated (if applicable).';
