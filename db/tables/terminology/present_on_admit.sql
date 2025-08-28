-- db/terminology/present_on_admit.sql
-- Terminology: Present on Admission (POA) code → description.
-- Uses psql var :"terminology_schema"

-- 1) Target shape
CREATE TABLE IF NOT EXISTS :"terminology_schema".present_on_admit (
  present_on_admit_code        varchar PRIMARY KEY,
  present_on_admit_description varchar
);

-- 2) Align columns for legacy installs (safe to re-run)
ALTER TABLE :"terminology_schema".present_on_admit
  ADD COLUMN IF NOT EXISTS present_on_admit_code        varchar,
  ADD COLUMN IF NOT EXISTS present_on_admit_description varchar;

-- 3) Helpful lookup index (optional)
CREATE INDEX IF NOT EXISTS present_on_admit_desc_idx
  ON :"terminology_schema".present_on_admit (present_on_admit_description);

-- 4) Docs
COMMENT ON TABLE  :"terminology_schema".present_on_admit
  IS 'Terminology: Present on Admission (POA) indicator codes.';
COMMENT ON COLUMN :"terminology_schema".present_on_admit.present_on_admit_code
  IS 'The code representing if the condition was present on admission.';
COMMENT ON COLUMN :"terminology_schema".present_on_admit.present_on_admit_description
  IS 'A description of the present on admission (POA) indicator.';
