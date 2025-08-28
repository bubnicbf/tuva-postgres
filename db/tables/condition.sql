-- db/tables/condition.sql
-- Core model: one record per condition/diagnosis occurrence.
-- Uses psql var :"schema" supplied by the wrapper.

CREATE TABLE IF NOT EXISTS :"schema".condition (
  condition_id                   varchar PRIMARY KEY,

  person_id                      varchar,        -- FK to patient
  member_id                      varchar,
  patient_id                     varchar,        -- clinical source id (no FK; resolve via crosswalk if desired)
  encounter_id                   varchar,        -- FK to encounter
  claim_id                       varchar,        -- free text; may join to medical_claim.claim_id (no FK to allow cross-system IDs)

  recorded_date                  date,
  onset_date                     date,
  resolved_date                  date,

  status                         varchar,        -- e.g., active, resolved, provisional
  condition_type                 varchar,        -- e.g., problem, admitting, billing

  -- Source coding (as received)
  source_code_type               varchar,        -- coded (terminology; e.g., ICD-10-CM, SNOMED)
  source_code                    varchar,        -- large dictionary (adapter-hydrated)
  source_description             varchar,

  -- Normalized coding (post-mapping)
  normalized_code_type           varchar,        -- coded (terminology)
  normalized_code                varchar,        -- large dictionary (adapter-hydrated)
  normalized_description         varchar,

  mapping_method                 varchar,        -- 'manual'|'automatic'|'custom' (free text)

  condition_rank                 integer,        -- 1 = principal dx, etc. (claims); optional/NA for EHR
  present_on_admit_code          varchar,        -- coded (terminology)
  present_on_admit_description   varchar,

  data_source                    varchar,
  tuva_last_run                  timestamp without time zone,

  -- Pragmatic DQ guards
  CONSTRAINT cond_date_order CHECK (
    (onset_date    IS NULL OR resolved_date IS NULL OR resolved_date >= onset_date)
    AND (recorded_date IS NULL OR onset_date IS NULL OR recorded_date >= onset_date)
  ),
  CONSTRAINT cond_rank_positive CHECK (
    condition_rank IS NULL OR condition_rank >= 1
  )
);

-- Foreign keys (deferrable so load order is flexible)
ALTER TABLE :"schema".condition
  ADD CONSTRAINT condition_person_fk
  FOREIGN KEY (person_id) REFERENCES :"schema".patient(person_id)
  DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE :"schema".condition
  ADD CONSTRAINT condition_encounter_fk
  FOREIGN KEY (encounter_id) REFERENCES :"schema".encounter(encounter_id)
  DEFERRABLE INITIALLY DEFERRED;

-- Helpful indexes
CREATE INDEX IF NOT EXISTS cond_person_idx           ON :"schema".condition (person_id);
CREATE INDEX IF NOT EXISTS cond_encounter_idx        ON :"schema".condition (encounter_id);
CREATE INDEX IF NOT EXISTS cond_claim_idx            ON :"schema".condition (claim_id);
CREATE INDEX IF NOT EXISTS cond_dates_idx            ON :"schema".condition (onset_date, resolved_date, recorded_date);
CREATE INDEX IF NOT EXISTS cond_source_code_idx      ON :"schema".condition (source_code_type, source_code);
CREATE INDEX IF NOT EXISTS cond_normalized_code_idx  ON :"schema".condition (normalized_code_type, normalized_code);
CREATE INDEX IF NOT EXISTS cond_poa_idx              ON :"schema".condition (present_on_admit_code);
CREATE INDEX IF NOT EXISTS cond_type_status_idx      ON :"schema".condition (condition_type, status);
