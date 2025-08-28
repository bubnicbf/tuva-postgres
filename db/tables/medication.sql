-- db/tables/medication.sql
-- Core model: one record per medication event (order/dispense/admin).
-- Uses psql var :"schema" supplied by the wrapper.

CREATE TABLE IF NOT EXISTS :"schema".medication (
  medication_id            varchar PRIMARY KEY,

  person_id                varchar,      -- FK to patient
  encounter_id             varchar,      -- FK to encounter

  dispensing_date          date,
  prescribing_date         date,

  -- Source coding (as received from the feed's own catalog)
  source_code_type         varchar,      -- coded (terminology: medication_code_type)
  source_code              varchar,      -- large/local dictionary (adapter-fed)
  source_description       varchar,

  -- Drug vocabularies (adapter-hydrated dictionaries)
  ndc_code                 varchar,      -- NDC-11 or formatted NDC
  ndc_description          varchar,
  ndc_mapping_method       varchar,      -- 'manual'|'automatic'|'custom'

  rxnorm_code              varchar,      -- RxCUI
  rxnorm_description       varchar,
  rxnorm_mapping_method    varchar,      -- 'manual'|'automatic'|'custom'

  atc_code                 varchar,      -- WHO ATC
  atc_description          varchar,
  atc_mapping_method       varchar,      -- 'manual'|'automatic'|'custom'

  route                    varchar,      -- e.g., oral, IV, IM, subcutaneous
  strength                 varchar,      -- free-text strength (preserve source)
  quantity                 numeric(18,3),
  quantity_unit            varchar,
  days_supply              numeric(18,3),

  practitioner_id          varchar,      -- FK to practitioner (core id)

  data_source              varchar,
  tuva_last_run            timestamp without time zone,

  -- Pragmatic DQ guards
  CONSTRAINT med_dates_order CHECK (
    prescribing_date IS NULL
    OR dispensing_date IS NULL
    OR dispensing_date >= prescribing_date
  ),
  CONSTRAINT med_nonneg_qty CHECK (
    (quantity     IS NULL OR quantity     >= 0) AND
    (days_supply  IS NULL OR days_supply  >= 0)
  )
);

-- Foreign keys (deferrable so load order is flexible)
ALTER TABLE :"schema".medication
  ADD CONSTRAINT med_person_fk
  FOREIGN KEY (person_id) REFERENCES :"schema".patient(person_id)
  DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE :"schema".medication
  ADD CONSTRAINT med_encounter_fk
  FOREIGN KEY (encounter_id) REFERENCES :"schema".encounter(encounter_id)
  DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE :"schema".medication
  ADD CONSTRAINT med_practitioner_fk
  FOREIGN KEY (practitioner_id) REFERENCES :"schema".practitioner(practitioner_id)
  DEFERRABLE INITIALLY DEFERRED;

-- Helpful indexes
CREATE INDEX IF NOT EXISTS med_person_idx        ON :"schema".medication (person_id);
CREATE INDEX IF NOT EXISTS med_encounter_idx     ON :"schema".medication (encounter_id);
CREATE INDEX IF NOT EXISTS med_dates_idx         ON :"schema".medication (prescribing_date, dispensing_date);
CREATE INDEX IF NOT EXISTS med_src_code_idx      ON :"schema".medication (source_code_type, source_code);
CREATE INDEX IF NOT EXISTS med_ndc_idx           ON :"schema".medication (ndc_code);
CREATE INDEX IF NOT EXISTS med_rxnorm_idx        ON :"schema".medication (rxnorm_code);
CREATE INDEX IF NOT EXISTS med_atc_idx           ON :"schema".medication (atc_code);
CREATE INDEX IF NOT EXISTS med_practitioner_idx  ON :"schema".medication (practitioner_id);
