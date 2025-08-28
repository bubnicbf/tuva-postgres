-- Terminology: Medicare Dual Eligibility (code → description)
-- Uses psql var :"terminology_schema"

CREATE TABLE IF NOT EXISTS :"terminology_schema".medicare_dual_eligibility (
  dual_status_code        varchar PRIMARY KEY,
  dual_status_description varchar
);

ALTER TABLE :"terminology_schema".medicare_dual_eligibility
  ADD COLUMN IF NOT EXISTS dual_status_code        varchar,
  ADD COLUMN IF NOT EXISTS dual_status_description varchar;

CREATE INDEX IF NOT EXISTS medicare_dual_elig_desc_idx
  ON :"terminology_schema".medicare_dual_eligibility (dual_status_description);

COMMENT ON TABLE  :"terminology_schema".medicare_dual_eligibility
  IS 'Terminology: Medicare dual-eligibility status codes.';
COMMENT ON COLUMN :"terminology_schema".medicare_dual_eligibility.dual_status_code
  IS 'The code representing dual eligibility status.';
COMMENT ON COLUMN :"terminology_schema".medicare_dual_eligibility.dual_status_description
  IS 'A description of the dual eligibility status.';
