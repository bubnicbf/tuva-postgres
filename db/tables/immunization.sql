-- db/tables/immunization.sql
-- Core model: one record per immunization event.
-- Uses psql var :"schema" supplied by the wrapper.

CREATE TABLE IF NOT EXISTS :"schema".immunization (
  immunization_id           varchar PRIMARY KEY,

  person_id                 varchar,        -- FK to patient
  patient_id                varchar,        -- clinical source id (no FK; resolve via crosswalk if desired)
  encounter_id              varchar,        -- FK to encounter

  -- Coding (source & normalized)
  source_code_type          varchar,        -- coded (terminology)
  source_code               varchar,        -- large dictionary (adapter-hydrated)
  source_description        varchar,

  normalized_code_type      varchar,        -- coded (terminology)
  normalized_code           varchar,        -- large dictionary (adapter-hydrated; typically CVX)
  normalized_description    varchar,

  status                    varchar,
  status_reason             varchar,

  occurrence_date           date,           -- administration (or intended) date

  source_dose               varchar,        -- keep text to preserve source fidelity
  normalized_dose           varchar,

  lot_number                varchar,
  body_site                 varchar,
  route                     varchar,

  location_id               varchar,        -- no FK (location table not defined here)
  practitioner_id           varchar,        -- FK to practitioner (core id)

  data_source               varchar,
  file_name                 varchar,
  ingest_datetime           timestamp without time zone,  -- "timestamp_ntz" per spec

  -- Light DQ guardrails
  CONSTRAINT imm_date_not_future CHECK (
    occurrence_date IS NULL OR occurrence_date <= CURRENT_DATE
  ),
  CONSTRAINT imm_ingest_not_future CHECK (
    ingest_datetime IS NULL OR ingest_datetime <= (NOW()::timestamp without time zone)
  )
);

-- Foreign keys (deferrable so load order is flexible)
ALTER TABLE :"schema".immunization
  ADD CONSTRAINT imm_person_fk
  FOREIGN KEY (person_id) REFERENCES :"schema".patient(person_id)
  DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE :"schema".immunization
  ADD CONSTRAINT imm_encounter_fk
  FOREIGN KEY (encounter_id) REFERENCES :"schema".encounter(encounter_id)
  DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE :"schema".immunization
  ADD CONSTRAINT imm_practitioner_fk
  FOREIGN KEY (practitioner_id) REFERENCES :"schema".practitioner(practitioner_id)
  DEFERRABLE INITIALLY DEFERRED;

-- Helpful indexes
CREATE INDEX IF NOT EXISTS imm_person_idx            ON :"schema".immunization (person_id);
CREATE INDEX IF NOT EXISTS imm_encounter_idx         ON :"schema".immunization (encounter_id);
CREATE INDEX IF NOT EXISTS imm_occurrence_date_idx   ON :"schema".immunization (occurrence_date);
CREATE INDEX IF NOT EXISTS imm_src_code_idx          ON :"schema".immunization (source_code_type, source_code);
CREATE INDEX IF NOT EXISTS imm_norm_code_idx         ON :"schema".immunization (normalized_code_type, normalized_code);
CREATE INDEX IF NOT EXISTS imm_practitioner_idx      ON :"schema".immunization (practitioner_id);
CREATE INDEX IF NOT EXISTS imm_location_idx          ON :"schema".immunization (location_id);
CREATE INDEX IF NOT EXISTS imm_ingest_dt_idx         ON :"schema".immunization (ingest_datetime);
CREATE INDEX IF NOT EXISTS imm_data_source_idx       ON :"schema".immunization (data_source);
