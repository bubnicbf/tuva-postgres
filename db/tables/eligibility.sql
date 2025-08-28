-- db/tables/eligibility.sql
-- Core model: one row per eligibility period (often monthly/continuous coverage).
-- Uses psql var :"schema" supplied by the wrapper.

CREATE TABLE IF NOT EXISTS :"schema".eligibility (
  eligibility_id                    varchar PRIMARY KEY,

  person_id                         varchar,      -- FK to patient
  member_id                         varchar,
  subscriber_id                     varchar,

  birth_date                        date,
  death_date                        date,

  enrollment_start_date             date,
  enrollment_end_date               date,

  payer                             varchar,
  payer_type                        varchar,      -- coded (terminology)
  plan                              varchar,

  original_reason_entitlement_code  varchar,      -- coded (terminology)
  dual_status_code                  varchar,      -- coded (terminology)
  medicare_status_code              varchar,      -- coded (terminology)

  subscriber_relation               varchar,

  group_id                          varchar,
  group_name                        varchar,

  normalized_state_name             varchar,
  fips_state_code                   varchar,
  fips_state_abbreviation           varchar,

  data_source                       varchar,
  file_date                         date,

  -- NOTE: spec defines tuva_last_run as string for eligibility
  tuva_last_run                     varchar,

  -- Light data quality checks
  CONSTRAINT elig_dates_order CHECK (
    enrollment_start_date IS NULL
    OR enrollment_end_date IS NULL
    OR enrollment_end_date >= enrollment_start_date
  ),
  CONSTRAINT elig_birth_before_death CHECK (
    birth_date IS NULL OR death_date IS NULL OR death_date >= birth_date
  )
);

-- Foreign key (deferrable so load order is flexible)
ALTER TABLE :"schema".eligibility
  ADD CONSTRAINT elig_person_fk
  FOREIGN KEY (person_id) REFERENCES :"schema".patient(person_id)
  DEFERRABLE INITIALLY DEFERRED;

-- Helpful indexes
CREATE INDEX IF NOT EXISTS elig_person_idx         ON :"schema".eligibility (person_id);
CREATE INDEX IF NOT EXISTS elig_member_idx         ON :"schema".eligibility (member_id, payer, plan);
CREATE INDEX IF NOT EXISTS elig_dates_idx          ON :"schema".eligibility (enrollment_start_date, enrollment_end_date);
CREATE INDEX IF NOT EXISTS elig_payer_type_idx     ON :"schema".eligibility (payer_type);
CREATE INDEX IF NOT EXISTS elig_state_idx          ON :"schema".eligibility (fips_state_code, fips_state_abbreviation);
