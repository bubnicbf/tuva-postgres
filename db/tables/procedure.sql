-- db/tables/procedure.sql
-- Core model: one record per performed procedure.
-- Uses psql var :"schema" supplied by the wrapper.

CREATE TABLE IF NOT EXISTS :"schema".procedure (
  procedure_id           varchar PRIMARY KEY,

  person_id              varchar,         -- FK to patient
  member_id              varchar,
  patient_id             varchar,         -- clinical source id (no FK; resolve via crosswalk)
  encounter_id           varchar,         -- FK to encounter
  claim_id               varchar,

  procedure_date         date,

  -- Source coding (as received)
  source_code_type       varchar,         -- coded (terminology: procedure_code_type)
  source_code            varchar,         -- large dictionary (adapter-hydrated)
  source_description     varchar,

  -- Normalized coding (post-mapping)
  normalized_code_type   varchar,         -- coded (terminology: procedure_code_type)
  normalized_code        varchar,         -- large dictionary (adapter-hydrated)
  normalized_description varchar,

  mapping_method         varchar,         -- 'manual'|'automatic'|'custom' (free text per spec)

  -- Modifiers (re-use HCPCS modifiers terminology table)
  modifier_1             varchar,
  modifier_2             varchar,
  modifier_3             varchar,
  modifier_4             varchar,
  modifier_5             varchar,

  practitioner_id        varchar,         -- FK to practitioner (core id)

  data_source            varchar,
  tuva_last_run          timestamp without time zone,

  -- Light DQ guards
  CONSTRAINT proc_date_not_future CHECK (
    procedure_date IS NULL OR procedure_date <= CURRENT_DATE
  )
);

-- Foreign keys (deferrable so load order is flexible)
ALTER TABLE :"schema".procedure
  ADD CONSTRAINT procedure_person_fk
  FOREIGN KEY (person_id) REFERENCES :"schema".patient(person_id)
  DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE :"schema".procedure
  ADD CONSTRAINT procedure_encounter_fk
  FOREIGN KEY (encounter_id) REFERENCES :"schema".encounter(encounter_id)
  DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE :"schema".procedure
  ADD CONSTRAINT procedure_practitioner_fk
  FOREIGN KEY (practitioner_id) REFERENCES :"schema".practitioner(practitioner_id)
  DEFERRABLE INITIALLY DEFERRED;

-- Helpful indexes
CREATE INDEX IF NOT EXISTS procedure_person_idx          ON :"schema".procedure (person_id);
CREATE INDEX IF NOT EXISTS procedure_encounter_idx       ON :"schema".procedure (encounter_id);
CREATE INDEX IF NOT EXISTS procedure_claim_idx           ON :"schema".procedure (claim_id);
CREATE INDEX IF NOT EXISTS procedure_date_idx            ON :"schema".procedure (procedure_date);
CREATE INDEX IF NOT EXISTS procedure_src_code_idx        ON :"schema".procedure (source_code_type, source_code);
CREATE INDEX IF NOT EXISTS procedure_norm_code_idx       ON :"schema".procedure (normalized_code_type, normalized_code);
CREATE INDEX IF NOT EXISTS procedure_practitioner_idx    ON :"schema".procedure (practitioner_id);
