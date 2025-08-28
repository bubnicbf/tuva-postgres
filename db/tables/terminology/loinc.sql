-- db/terminology/loinc.sql
-- Terminology: LOINC codes and rich attributes.
-- Uses psql var :"terminology_schema"

-- 1) Target table
CREATE TABLE IF NOT EXISTS :"terminology_schema".loinc (
  loinc                  varchar PRIMARY KEY,  -- e.g., '2345-7'
  short_name             varchar,              -- short human-readable description
  long_common_name       varchar,              -- clinician-friendly full name
  component              varchar,              -- part 1 of 6
  property               varchar,              -- part 2 of 6
  time_aspect            varchar,              -- part 3 of 6
  system                 varchar,              -- part 4 of 6 (specimen/subject)
  scale_type             varchar,              -- part 5 of 6
  method_type            varchar,              -- part 6 of 6 (optional)
  class_code             varchar,              -- e.g., 'CHEM'
  class_description      varchar,              -- e.g., 'Chemistry'
  class_type_code        varchar,              -- top-level category code
  class_type_description varchar,              -- top-level category description
  paneltype              varchar,
  order_obs              varchar,
  example_units          varchar,
  external_copyright_notice varchar,
  status                 varchar,              -- Active, Trial, Discouraged, Deprecated
  version_first_released varchar,
  version_last_changed   varchar
);

-- 2) Align columns for legacy installs (no-op if present)
ALTER TABLE :"terminology_schema".loinc
  ADD COLUMN IF NOT EXISTS loinc                  varchar,
  ADD COLUMN IF NOT EXISTS short_name             varchar,
  ADD COLUMN IF NOT EXISTS long_common_name       varchar,
  ADD COLUMN IF NOT EXISTS component              varchar,
  ADD COLUMN IF NOT EXISTS property               varchar,
  ADD COLUMN IF NOT EXISTS time_aspect            varchar,
  ADD COLUMN IF NOT EXISTS system                 varchar,
  ADD COLUMN IF NOT EXISTS scale_type             varchar,
  ADD COLUMN IF NOT EXISTS method_type            varchar,
  ADD COLUMN IF NOT EXISTS class_code             varchar,
  ADD COLUMN IF NOT EXISTS class_description      varchar,
  ADD COLUMN IF NOT EXISTS class_type_code        varchar,
  ADD COLUMN IF NOT EXISTS class_type_description varchar,
  ADD COLUMN IF NOT EXISTS paneltype              varchar,
  ADD COLUMN IF NOT EXISTS order_obs              varchar,
  ADD COLUMN IF NOT EXISTS example_units          varchar,
  ADD COLUMN IF NOT EXISTS external_copyright_notice varchar,
  ADD COLUMN IF NOT EXISTS status                 varchar,
  ADD COLUMN IF NOT EXISTS version_first_released varchar,
  ADD COLUMN IF NOT EXISTS version_last_changed   varchar;

-- 3) (Optional) normalize a few legacy column names if they exist
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = :'terminology_schema' AND table_name = 'loinc' AND column_name = 'order_obs')
  THEN NULL; END IF;
END$$;

-- 4) Helpful lookup indexes
CREATE INDEX IF NOT EXISTS loinc_short_name_idx
  ON :"terminology_schema".loinc (short_name);
CREATE INDEX IF NOT EXISTS loinc_class_code_idx
  ON :"terminology_schema".loinc (class_code);
CREATE INDEX IF NOT EXISTS loinc_status_idx
  ON :"terminology_schema".loinc (status);

-- 5) Documentation
COMMENT ON TABLE  :"terminology_schema".loinc IS 'LOINC terminology with core 6-part attributes and friendly names.';
COMMENT ON COLUMN :"terminology_schema".loinc.loinc                  IS 'The LOINC code.';
COMMENT ON COLUMN :"terminology_schema".loinc.short_name             IS 'Short human-readable description (if present).';
COMMENT ON COLUMN :"terminology_schema".loinc.long_common_name       IS 'Clinician-friendly full description.';
COMMENT ON COLUMN :"terminology_schema".loinc.component              IS 'LOINC Part 1 (Analyte).';
COMMENT ON COLUMN :"terminology_schema".loinc.property               IS 'LOINC Part 2 (Property).';
COMMENT ON COLUMN :"terminology_schema".loinc.time_aspect            IS 'LOINC Part 3 (Time).';
COMMENT ON COLUMN :"terminology_schema".loinc.system                 IS 'LOINC Part 4 (System/Specimen).';
COMMENT ON COLUMN :"terminology_schema".loinc.scale_type             IS 'LOINC Part 5 (Scale).';
COMMENT ON COLUMN :"terminology_schema".loinc.method_type            IS 'LOINC Part 6 (Method).';
COMMENT ON COLUMN :"terminology_schema".loinc.class_code             IS 'General LOINC class code.';
COMMENT ON COLUMN :"terminology_schema".loinc.class_description      IS 'General LOINC class description.';
COMMENT ON COLUMN :"terminology_schema".loinc.class_type_code        IS 'Top-level category code.';
COMMENT ON COLUMN :"terminology_schema".loinc.class_type_description IS 'Top-level category description.';
COMMENT ON COLUMN :"terminology_schema".loinc.paneltype              IS 'LOINC panel indicator (if provided by source).';
COMMENT ON COLUMN :"terminology_schema".loinc.order_obs              IS 'Indicates order vs. observation (if provided).';
COMMENT ON COLUMN :"terminology_schema".loinc.example_units          IS 'Example reporting units.';
COMMENT ON COLUMN :"terminology_schema".loinc.external_copyright_notice IS 'External copyright notice.';
COMMENT ON COLUMN :"terminology_schema".loinc.status                 IS 'Concept status: Active, Trial, Discouraged, Deprecated.';
COMMENT ON COLUMN :"terminology_schema".loinc.version_first_released IS 'First LOINC release containing this code.';
COMMENT ON COLUMN :"terminology_schema".loinc.version_last_changed   IS 'Most recent LOINC release modifying this record.';
