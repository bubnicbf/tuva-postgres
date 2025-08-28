-- db/tables/appointment.sql
-- Core model: one record per appointment instance.
-- Uses psql var :"schema" supplied by the wrapper.

CREATE TABLE IF NOT EXISTS :"schema".appointment (
  appointment_id                           varchar PRIMARY KEY,

  person_id                                 varchar,      -- FK -> patient
  patient_id                                varchar,      -- clinical source id (no FK)
  encounter_id                              varchar,      -- FK -> encounter (if realized as a visit)

  -- Appointment type (source & normalized)
  source_appointment_type_code              varchar,      -- coded (terminology)
  source_appointment_type_description       varchar,
  normalized_appointment_type_code          varchar,      -- coded (terminology)
  normalized_appointment_type_description   varchar,

  -- Timing
  start_datetime                            timestamp without time zone,
  end_datetime                              timestamp without time zone,
  duration                                  numeric(10,2),  -- minutes

  -- Context
  location_id                               varchar,      -- (no FK in this repo)
  practitioner_id                           varchar,      -- FK -> practitioner

  -- Status (source & normalized)
  source_status                             varchar,      -- coded (terminology)
  normalized_status                         varchar,      -- coded (terminology)

  appointment_specialty                     varchar,
  reason                                    varchar,

  -- Reason code (source & normalized)
  source_reason_code_type                   varchar,      -- coded (terminology)
  source_reason_code                        varchar,      -- large dictionary (adapter-hydrated)
  source_reason_description                 varchar,
  normalized_reason_code_type               varchar,      -- coded (terminology)
  normalized_reason_code                    varchar,      -- large dictionary (adapter-hydrated)
  normalized_reason_description             varchar,

  -- Cancellation
  cancellation_reason                       varchar,
  source_cancellation_reason_code_type      varchar,      -- coded (terminology)
  source_cancellation_reason_code           varchar,      -- large dictionary (adapter-hydrated)
  source_cancellation_reason_description    varchar,
  normalized_cancellation_reason_code_type  varchar,      -- coded (terminology)
  normalized_cancellation_reason_code       varchar,      -- large dictionary (adapter-hydrated)
  normalized_cancellation_reason_description varchar,

  mapping_method                            varchar,      -- 'manual' | 'automatic' | 'custom'

  data_source                               varchar,
  tuva_last_run                             timestamp without time zone,

  -- Pragmatic DQ guards
  CONSTRAINT appt_end_ge_start CHECK (
    start_datetime IS NULL
    OR end_datetime IS NULL
    OR end_datetime >= start_datetime
  ),
  CONSTRAINT appt_duration_nonneg CHECK (
    duration IS NULL OR duration >= 0
  )
  -- (Optional) If you want strict duration match, enable this guard instead:
  -- ,CONSTRAINT appt_duration_matches CHECK (
  --   start_datetime IS NULL OR end_datetime IS NULL OR duration IS NULL
  --   OR duration = ROUND(EXTRACT(EPOCH FROM (end_datetime - start_datetime)) / 60.0, 2)
  -- )
);

-- Foreign keys (deferrable so load order is flexible)
ALTER TABLE :"schema".appointment
  ADD CONSTRAINT appt_person_fk
  FOREIGN KEY (person_id) REFERENCES :"schema".patient(person_id)
  DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE :"schema".appointment
  ADD CONSTRAINT appt_encounter_fk
  FOREIGN KEY (encounter_id) REFERENCES :"schema".encounter(encounter_id)
  DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE :"schema".appointment
  ADD CONSTRAINT appt_practitioner_fk
  FOREIGN KEY (practitioner_id) REFERENCES :"schema".practitioner(practitioner_id)
  DEFERRABLE INITIALLY DEFERRED;

-- Helpful indexes
CREATE INDEX IF NOT EXISTS appt_person_idx          ON :"schema".appointment (person_id);
CREATE INDEX IF NOT EXISTS appt_encounter_idx       ON :"schema".appointment (encounter_id);
CREATE INDEX IF NOT EXISTS appt_time_idx            ON :"schema".appointment (start_datetime, end_datetime);
CREATE INDEX IF NOT EXISTS appt_location_idx        ON :"schema".appointment (location_id);
CREATE INDEX IF NOT EXISTS appt_practitioner_idx    ON :"schema".appointment (practitioner_id);
CREATE INDEX IF NOT EXISTS appt_src_type_idx        ON :"schema".appointment (source_appointment_type_code);
CREATE INDEX IF NOT EXISTS appt_norm_type_idx       ON :"schema".appointment (normalized_appointment_type_code);
CREATE INDEX IF NOT EXISTS appt_src_reason_idx      ON :"schema".appointment (source_reason_code_type, source_reason_code);
CREATE INDEX IF NOT EXISTS appt_norm_reason_idx     ON :"schema".appointment (normalized_reason_code_type, normalized_reason_code);
CREATE INDEX IF NOT EXISTS appt_src_cancel_idx      ON :"schema".appointment (source_cancellation_reason_code_type, source_cancellation_reason_code);
CREATE INDEX IF NOT EXISTS appt_norm_cancel_idx     ON :"schema".appointment (normalized_cancellation_reason_code_type, normalized_cancellation_reason_code);
CREATE INDEX IF NOT EXISTS appt_status_idx          ON :"schema".appointment (source_status, normalized_status);
CREATE INDEX IF NOT EXISTS appt_data_source_idx     ON :"schema".appointment (data_source);
