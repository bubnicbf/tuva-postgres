-- db/tables/medical_claim.sql
-- Core model: one row per claim line (or claim header if line_number is 1-only feeds).
-- Uses psql var :"schema" supplied by the wrapper.

CREATE TABLE IF NOT EXISTS :"schema".medical_claim (
  medical_claim_id              varchar PRIMARY KEY,  -- unique row id

  claim_id                      varchar,              -- claim grouping id
  claim_line_number             integer,              -- line within claim

  encounter_id                  varchar,              -- FK to encounter
  encounter_type                varchar,              -- free text (claims-derived)
  encounter_group               varchar,

  claim_type                    varchar,              -- coded (terminology)
  person_id                     varchar,              -- FK to patient
  member_id                     varchar,
  payer                         varchar,
  plan                          varchar,

  claim_start_date              date,
  claim_end_date                date,
  claim_line_start_date         date,
  claim_line_end_date           date,
  admission_date                date,
  discharge_date                date,

  service_category_1            varchar,              -- coded (terminology)
  service_category_2            varchar,              -- coded (terminology)
  service_category_3            varchar,              -- free text (most specific)

  admit_source_code             varchar,              -- coded (terminology)
  admit_source_description      varchar,
  admit_type_code               varchar,              -- coded (terminology)
  admit_type_description        varchar,
  discharge_disposition_code    varchar,              -- coded (terminology)
  discharge_disposition_description varchar,

  place_of_service_code         varchar,              -- coded (terminology)
  place_of_service_description  varchar,

  bill_type_code                varchar,              -- coded (terminology)
  bill_type_description         varchar,

  drg_code_type                 varchar,
  drg_code                      varchar,
  drg_description               varchar,

  revenue_center_code           varchar,              -- coded (terminology)
  revenue_center_description    varchar,
  service_unit_quantity         numeric(18,3),

  hcpcs_code                    varchar,              -- CPT/HCPCS (terminology, large)
  hcpcs_modifier_1              varchar,              -- modifiers (terminology)
  hcpcs_modifier_2              varchar,
  hcpcs_modifier_3              varchar,
  hcpcs_modifier_4              varchar,
  hcpcs_modifier_5              varchar,

  rendering_id                  varchar,              -- large terminology (adapter-fed)
  rendering_tin                 varchar,
  rendering_name                varchar,

  billing_id                    varchar,              -- large terminology (adapter-fed)
  billing_tin                   varchar,
  billing_name                  varchar,

  facility_id                   varchar,              -- large terminology (adapter-fed)
  facility_name                 varchar,

  paid_date                     date,
  paid_amount                   numeric(18,2),
  allowed_amount                numeric(18,2),
  charge_amount                 numeric(18,2),
  coinsurance_amount            numeric(18,2),
  copayment_amount              numeric(18,2),
  deductible_amount             numeric(18,2),
  total_cost_amount             numeric(18,2),

  in_network_flag               integer,              -- 0/1
  enrollment_flag               integer,              -- 0/1

  member_month_key              bigint,
  data_source                   varchar,
  file_date                     date,
  tuva_last_run                 timestamp without time zone,

  -- Quality guards (light-weight; avoid over-constraining real-world data)
  CONSTRAINT mc_flags_boolish CHECK (
    (in_network_flag IN (0,1) OR in_network_flag IS NULL) AND
    (enrollment_flag IN (0,1) OR enrollment_flag IS NULL)
  ),
  CONSTRAINT mc_date_order_claim CHECK (
    claim_start_date IS NULL OR claim_end_date IS NULL OR claim_end_date >= claim_start_date
  ),
  CONSTRAINT mc_date_order_line CHECK (
    claim_line_start_date IS NULL OR claim_line_end_date IS NULL OR claim_line_end_date >= claim_line_start_date
  ),
  CONSTRAINT mc_date_order_adm CHECK (
    admission_date IS NULL OR discharge_date IS NULL OR discharge_date >= admission_date
  )
);

-- Foreign keys (deferrable so you can load in any order)
ALTER TABLE :"schema".medical_claim
  ADD CONSTRAINT mc_person_fk
  FOREIGN KEY (person_id) REFERENCES :"schema".patient(person_id)
  DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE :"schema".medical_claim
  ADD CONSTRAINT mc_encounter_fk
  FOREIGN KEY (encounter_id) REFERENCES :"schema".encounter(encounter_id)
  DEFERRABLE INITIALLY DEFERRED;

-- Helpful indexes
CREATE INDEX IF NOT EXISTS mc_claim_header_idx   ON :"schema".medical_claim (claim_id, claim_line_number);
CREATE INDEX IF NOT EXISTS mc_person_idx         ON :"schema".medical_claim (person_id);
CREATE INDEX IF NOT EXISTS mc_member_idx         ON :"schema".medical_claim (member_id, payer, plan);
CREATE INDEX IF NOT EXISTS mc_encounter_idx      ON :"schema".medical_claim (encounter_id);
CREATE INDEX IF NOT EXISTS mc_dates_idx          ON :"schema".medical_claim (claim_start_date, claim_end_date);
CREATE INDEX IF NOT EXISTS mc_paid_date_idx      ON :"schema".medical_claim (paid_date);
CREATE INDEX IF NOT EXISTS mc_pos_idx            ON :"schema".medical_claim (place_of_service_code);
CREATE INDEX IF NOT EXISTS mc_rev_center_idx     ON :"schema".medical_claim (revenue_center_code);
CREATE INDEX IF NOT EXISTS mc_hcpcs_idx          ON :"schema".medical_claim (hcpcs_code);
CREATE INDEX IF NOT EXISTS mc_claim_type_idx     ON :"schema".medical_claim (claim_type);
