-- db/tables/encounter.sql
-- Core model: one record per encounter.
-- Uses psql var :"schema" supplied by the wrapper.

CREATE TABLE IF NOT EXISTS :"schema".encounter (
  "_DBT_SOURCE_RELATION"         varchar,  -- optional lineage field

  encounter_id                   varchar PRIMARY KEY,
  person_id                      varchar NOT NULL,       -- FK to patient
  encounter_type                 varchar,                -- coded (terminology)
  encounter_group                varchar,

  encounter_start_date           date,
  encounter_end_date             date,
  length_of_stay                 integer,                -- days (end - start)

  admit_source_code              varchar,                -- coded (terminology)
  admit_source_description       varchar,
  admit_type_code                varchar,                -- coded (terminology)
  admit_type_description         varchar,
  discharge_disposition_code     varchar,                -- coded (terminology)
  discharge_disposition_description varchar,

  attending_provider_id          varchar,                -- FK to practitioner
  attending_provider_name        varchar,

  facility_id                    varchar,
  facility_name                  varchar,
  facility_type                  varchar,

  observation_flag               integer,
  lab_flag                       integer,
  dme_flag                       integer,
  ambulance_flag                 integer,
  pharmacy_flag                  integer,
  ed_flag                        integer,
  delivery_flag                  integer,
  delivery_type                  varchar,
  newborn_flag                   integer,
  nicu_flag                      integer,
  snf_part_b_flag                integer,

  primary_diagnosis_code_type    varchar,                -- coded (terminology)
  primary_diagnosis_code         varchar,                -- coded (big dictionary from cloud)
  primary_diagnosis_description  varchar,

  drg_code_type                  varchar,
  drg_code                       varchar,
  drg_description                varchar,

  paid_amount                    numeric(18,2),
  allowed_amount                 numeric(18,2),
  charge_amount                  numeric(18,2),

  claim_count                    integer,
  inst_claim_count               integer,
  prof_claim_count               integer,

  source_model                   varchar,                -- lineage hint (dbt source relation name)
  data_source                    varchar,
  encounter_source_type          varchar,                -- 'claims' | 'clinical' (free text)

  tuva_last_run                  timestamp without time zone,

  -- Quality guards
  CONSTRAINT enc_dates_order CHECK (
    encounter_start_date IS NULL
    OR encounter_end_date   IS NULL
    OR encounter_end_date >= encounter_start_date
  ),
  CONSTRAINT enc_los_nonneg CHECK (
    length_of_stay IS NULL OR length_of_stay >= 0
  ),
  CONSTRAINT enc_flags_bool CHECK (
    (observation_flag IN (0,1) OR observation_flag IS NULL) AND
    (lab_flag         IN (0,1) OR lab_flag         IS NULL) AND
    (dme_flag         IN (0,1) OR dme_flag         IS NULL) AND
    (ambulance_flag   IN (0,1) OR ambulance_flag   IS NULL) AND
    (pharmacy_flag    IN (0,1) OR pharmacy_flag    IS NULL) AND
    (ed_flag          IN (0,1) OR ed_flag          IS NULL) AND
    (delivery_flag    IN (0,1) OR delivery_flag    IS NULL) AND
    (newborn_flag     IN (0,1) OR newborn_flag     IS NULL) AND
    (nicu_flag        IN (0,1) OR nicu_flag        IS NULL) AND
    (snf_part_b_flag  IN (0,1) OR snf_part_b_flag  IS NULL)
  )
);

-- Foreign keys (enable if your load order guarantees referential presence)
ALTER TABLE :"schema".encounter
  ADD CONSTRAINT encounter_person_fk
  FOREIGN KEY (person_id) REFERENCES :"schema".patient(person_id)
  DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE :"schema".encounter
  ADD CONSTRAINT encounter_attending_pr_fk
  FOREIGN KEY (attending_provider_id) REFERENCES :"schema".practitioner(practitioner_id)
  DEFERRABLE INITIALLY DEFERRED;

-- Helpful indexes
CREATE INDEX IF NOT EXISTS enc_person_idx            ON :"schema".encounter (person_id);
CREATE INDEX IF NOT EXISTS enc_dates_idx             ON :"schema".encounter (encounter_start_date, encounter_end_date);
CREATE INDEX IF NOT EXISTS enc_type_idx              ON :"schema".encounter (encounter_type);
CREATE INDEX IF NOT EXISTS enc_facility_idx          ON :"schema".encounter (facility_id);
CREATE INDEX IF NOT EXISTS enc_attending_idx         ON :"schema".encounter (attending_provider_id);
CREATE INDEX IF NOT EXISTS enc_ed_flag_idx           ON :"schema".encounter (ed_flag);
CREATE INDEX IF NOT EXISTS enc_primary_dx_idx        ON :"schema".encounter (primary_diagnosis_code);
