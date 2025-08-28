-- db/tables/pharmacy_claim.sql
-- Core model: one row per pharmacy claim line.
-- Uses psql var :"schema" supplied by the wrapper.

CREATE TABLE IF NOT EXISTS :"schema".pharmacy_claim (
  pharmacy_claim_id            varchar PRIMARY KEY,

  claim_id                     varchar,
  claim_line_number            integer,

  person_id                    varchar,            -- FK to patient
  member_id                    varchar,
  payer                        varchar,
  plan                         varchar,

  prescribing_provider_id      varchar,            -- large terminology (adapter-fed)
  prescribing_provider_name    varchar,
  dispensing_provider_id       varchar,            -- large terminology (adapter-fed)
  dispensing_provider_name     varchar,

  dispensing_date              date,

  ndc_code                     varchar,            -- terminology (NDC dictionary)
  ndc_description              varchar,

  quantity                     integer,
  days_supply                  integer,
  refills                      integer,

  paid_date                    date,
  paid_amount                  numeric(18,2),
  allowed_amount               numeric(18,2),
  charge_amount                numeric(18,2),
  coinsurance_amount           numeric(18,2),
  copayment_amount             numeric(18,2),
  deductible_amount            numeric(18,2),

  in_network_flag              integer,            -- 0/1
  enrollment_flag              integer,            -- 0/1

  member_month_key             bigint,
  data_source                  varchar,
  file_date                    date,

  -- NOTE: spec uses varchar for tuva_last_run here (dbt pretty_time string)
  tuva_last_run                varchar,

  -- Light DQ guards
  CONSTRAINT rx_flags_boolish CHECK (
    (in_network_flag IN (0,1) OR in_network_flag IS NULL) AND
    (enrollment_flag IN (0,1) OR enrollment_flag IS NULL)
  ),
  CONSTRAINT rx_nonneg_qty CHECK (
    (quantity    IS NULL OR quantity    >= 0) AND
    (days_supply IS NULL OR days_supply >= 0) AND
    (refills     IS NULL OR refills     >= 0)
  ),
  CONSTRAINT rx_money_nonneg CHECK (
    (paid_amount        IS NULL OR paid_amount        >= 0) AND
    (allowed_amount     IS NULL OR allowed_amount     >= 0) AND
    (charge_amount      IS NULL OR charge_amount      >= 0) AND
    (coinsurance_amount IS NULL OR coinsurance_amount >= 0) AND
    (copayment_amount   IS NULL OR copayment_amount   >= 0) AND
    (deductible_amount  IS NULL OR deductible_amount  >= 0)
  )
);

-- Foreign key (deferrable so load order is flexible)
ALTER TABLE :"schema".pharmacy_claim
  ADD CONSTRAINT rx_person_fk
  FOREIGN KEY (person_id) REFERENCES :"schema".patient(person_id)
  DEFERRABLE INITIALLY DEFERRED;

-- Helpful indexes
CREATE INDEX IF NOT EXISTS rx_person_idx            ON :"schema".pharmacy_claim (person_id);
CREATE INDEX IF NOT EXISTS rx_member_idx            ON :"schema".pharmacy_claim (member_id, payer, plan);
CREATE INDEX IF NOT EXISTS rx_dispense_date_idx     ON :"schema".pharmacy_claim (dispensing_date);
CREATE INDEX IF NOT EXISTS rx_paid_date_idx         ON :"schema".pharmacy_claim (paid_date);
CREATE INDEX IF NOT EXISTS rx_claim_tuple_idx       ON :"schema".pharmacy_claim (claim_id, claim_line_number);
CREATE INDEX IF NOT EXISTS rx_ndc_idx               ON :"schema".pharmacy_claim (ndc_code);
CREATE INDEX IF NOT EXISTS rx_prescribing_id_idx    ON :"schema".pharmacy_claim (prescribing_provider_id);
CREATE INDEX IF NOT EXISTS rx_dispensing_id_idx     ON :"schema".pharmacy_claim (dispensing_provider_id);
