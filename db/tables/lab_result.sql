-- db/tables/lab_result.sql
-- Core model: one record per lab result component (granular).
-- Uses psql var :"schema" supplied by the wrapper.

CREATE TABLE IF NOT EXISTS :"schema".lab_result (
  lab_result_id                       varchar PRIMARY KEY,

  person_id                           varchar,      -- FK to patient (deferrable)
  patient_id                          varchar,      -- clinical source id (no FK; resolve via crosswalk if desired)
  encounter_id                        varchar,      -- FK to encounter (deferrable)

  accession_number                    varchar,      -- lab order number in source

  -- Order (panel/test) coding
  source_order_type                   varchar,      -- coded (terminology: small set)
  source_order_code                   varchar,      -- large dictionary (adapter-fed)
  source_order_description            varchar,

  -- Component (analyte) coding
  source_component_type               varchar,      -- free text per spec
  source_component_code               varchar,      -- large dictionary (adapter-fed)
  source_component_description        varchar,

  -- Normalized order coding
  normalized_order_type               varchar,      -- coded (terminology: small set)
  normalized_order_code               varchar,      -- large dictionary (adapter-fed)
  normalized_order_description        varchar,

  -- Normalized component coding
  normalized_component_type           varchar,      -- free text per spec
  normalized_component_code           varchar,      -- large dictionary (adapter-fed)
  normalized_component_description    varchar,

  mapping_method                      varchar,      -- 'manual'|'automatic'|'custom' (free text)

  status                              varchar,      -- e.g., 'final','corrected','prelim'
  result                              varchar,      -- keep as text to preserve source fidelity

  result_datetime                     timestamp without time zone,
  collection_datetime                 timestamp without time zone,

  source_units                        varchar,
  normalized_units                    varchar,

  source_reference_range_low          varchar,
  source_reference_range_high         varchar,
  normalized_reference_range_low      varchar,
  normalized_reference_range_high     varchar,

  source_abnormal_flag                varchar,
  normalized_abnormal_flag            varchar,

  specimen                            varchar,      -- e.g., 'blood','urine','plasma'

  ordering_practitioner_id            varchar,      -- adapter-fed directory id (no FK)
  data_source                         varchar,
  tuva_last_run                       timestamp without time zone,

  -- Light DQ guardrails (pragmatic)
  CONSTRAINT lab_dates_order CHECK (
    collection_datetime IS NULL
    OR result_datetime  IS NULL
    OR result_datetime  >= collection_datetime
  )
);

-- Foreign keys (deferrable so load order is flexible)
ALTER TABLE :"schema".lab_result
  ADD CONSTRAINT lab_result_person_fk
  FOREIGN KEY (person_id)   REFERENCES :"schema".patient(person_id)
  DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE :"schema".lab_result
  ADD CONSTRAINT lab_result_encounter_fk
  FOREIGN KEY (encounter_id) REFERENCES :"schema".encounter(encounter_id)
  DEFERRABLE INITIALLY DEFERRED;

-- Helpful indexes
CREATE INDEX IF NOT EXISTS lab_person_idx            ON :"schema".lab_result (person_id);
CREATE INDEX IF NOT EXISTS lab_encounter_idx         ON :"schema".lab_result (encounter_id);
CREATE INDEX IF NOT EXISTS lab_accession_idx         ON :"schema".lab_result (accession_number);
CREATE INDEX IF NOT EXISTS lab_result_dt_idx         ON :"schema".lab_result (result_datetime);
CREATE INDEX IF NOT EXISTS lab_collect_dt_idx        ON :"schema".lab_result (collection_datetime);
CREATE INDEX IF NOT EXISTS lab_source_order_idx      ON :"schema".lab_result (source_order_type, source_order_code);
CREATE INDEX IF NOT EXISTS lab_source_comp_idx       ON :"schema".lab_result (source_component_type, source_component_code);
CREATE INDEX IF NOT EXISTS lab_norm_order_idx        ON :"schema".lab_result (normalized_order_type, normalized_order_code);
CREATE INDEX IF NOT EXISTS lab_norm_comp_idx         ON :"schema".lab_result (normalized_component_type, normalized_component_code);
CREATE INDEX IF NOT EXISTS lab_practitioner_idx      ON :"schema".lab_result (ordering_practitioner_id);
CREATE INDEX IF NOT EXISTS lab_data_source_idx       ON :"schema".lab_result (data_source);
