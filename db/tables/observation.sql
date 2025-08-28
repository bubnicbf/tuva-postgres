-- db/tables/observation.sql
-- Core model: one record per clinical observation (labs, vitals, etc.)
-- Uses psql var :"schema" supplied by the wrapper.

CREATE TABLE IF NOT EXISTS :"schema".observation (
  observation_id                    varchar PRIMARY KEY,

  person_id                         varchar,      -- FK to patient
  patient_id                        varchar,      -- clinical source id (no FK; resolve via crosswalk if needed)
  encounter_id                      varchar,      -- FK to encounter

  panel_id                          varchar,
  observation_date                  date,
  observation_type                  varchar,      -- e.g., lab, vital, imaging_result, etc.

  -- Source coding (as received)
  source_code_type                  varchar,      -- coded (terminology)
  source_code                       varchar,
  source_description                varchar,

  -- Normalized coding (post-mapping; free text per spec)
  normalized_code_type              varchar,
  normalized_code                   varchar,
  normalized_description            varchar,
  mapping_method                    varchar,      -- 'manual'|'automatic'|'custom' (free text per spec)

  -- Results & units (stored as text to preserve source fidelity)
  result                            varchar,
  source_units                      varchar,
  normalized_units                  varchar,

  source_reference_range_low        varchar,
  source_reference_range_high       varchar,
  normalized_reference_range_low    varchar,
  normalized_reference_range_high   varchar,

  data_source                       varchar,
  tuva_last_run                     timestamp without time zone,

  -- Light DQ guardrails
  CONSTRAINT obs_date_not_future CHECK (
    observation_date IS NULL OR observation_date <= CURRENT_DATE
  )
);

-- Foreign keys (deferrable so load order is flexible)
ALTER TABLE :"schema".observation
  ADD CONSTRAINT observation_person_fk
  FOREIGN KEY (person_id) REFERENCES :"schema".patient(person_id)
  DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE :"schema".observation
  ADD CONSTRAINT observation_encounter_fk
  FOREIGN KEY (encounter_id) REFERENCES :"schema".encounter(encounter_id)
  DEFERRABLE INITIALLY DEFERRED;

-- Helpful indexes
CREATE INDEX IF NOT EXISTS obs_person_idx           ON :"schema".observation (person_id);
CREATE INDEX IF NOT EXISTS obs_encounter_idx        ON :"schema".observation (encounter_id);
CREATE INDEX IF NOT EXISTS obs_date_idx             ON :"schema".observation (observation_date);
CREATE INDEX IF NOT EXISTS obs_panel_idx            ON :"schema".observation (panel_id);
CREATE INDEX IF NOT EXISTS obs_type_idx             ON :"schema".observation (observation_type);
CREATE INDEX IF NOT EXISTS obs_source_code_idx      ON :"schema".observation (source_code_type, source_code);
CREATE INDEX IF NOT EXISTS obs_normalized_code_idx  ON :"schema".observation (normalized_code_type, normalized_code);
