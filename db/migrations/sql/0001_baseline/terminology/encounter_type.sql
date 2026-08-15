-- db/terminology/encounter_type.sql
-- Terminology: Encounter Group → Encounter Type pairs.
-- Uses psql var :"terminology_schema" supplied by the wrapper.

-- 1) Create table to target shape (safe to re-run)
CREATE TABLE IF NOT EXISTS :"terminology_schema".encounter_type (
  encounter_group varchar NOT NULL,
  encounter_type  varchar NOT NULL,
  CONSTRAINT encounter_type_pk PRIMARY KEY (encounter_group, encounter_type)
);

-- 2) Align columns for legacy installs (no-op if already present)
ALTER TABLE :"terminology_schema".encounter_type
  ADD COLUMN IF NOT EXISTS encounter_group varchar,
  ADD COLUMN IF NOT EXISTS encounter_type  varchar;

-- 3) Helpful index for group lookups (optional)
CREATE INDEX IF NOT EXISTS encounter_type_group_idx
  ON :"terminology_schema".encounter_type (encounter_group);

-- 4) Docs
COMMENT ON TABLE  :"terminology_schema".encounter_type
  IS 'Terminology: valid (encounter_group, encounter_type) pairs.';
COMMENT ON COLUMN :"terminology_schema".encounter_type.encounter_group
  IS 'The group of the encounter.';
COMMENT ON COLUMN :"terminology_schema".encounter_type.encounter_type
  IS 'The type of the encounter.';
