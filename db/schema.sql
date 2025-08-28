-- Create schema if not exists is done in the loader; safe here too:
CREATE SCHEMA IF NOT EXISTS :PG_SCHEMA;  -- ignored by psql, but left as note

-- Example tables (adjust to real Tuva columns)
CREATE TABLE IF NOT EXISTS tuva.patient (
  patient_id        text PRIMARY KEY,
  birth_date        date,
  sex               text,
  race              text,
  ethnicity         text
);

CREATE TABLE IF NOT EXISTS tuva.payer (
  payer_id          text PRIMARY KEY,
  payer_name        text
);

CREATE TABLE IF NOT EXISTS tuva.provider (
  provider_id       text PRIMARY KEY,
  npi               text,
  provider_name     text
);

CREATE TABLE IF NOT EXISTS tuva.encounter (
  encounter_id      text PRIMARY KEY,
  patient_id        text NOT NULL,
  provider_id       text,
  encounter_date    date,
  CONSTRAINT enc_patient_fk FOREIGN KEY (patient_id) REFERENCES tuva.patient(patient_id)
);

CREATE TABLE IF NOT EXISTS tuva.claim (
  claim_id          text PRIMARY KEY,
  encounter_id      text,
  payer_id          text,
  claim_status      text,
  total_charge_amt  numeric,
  total_paid_amt    numeric,
  CONSTRAINT claim_enc_fk FOREIGN KEY (encounter_id) REFERENCES tuva.encounter(encounter_id),
  CONSTRAINT claim_payer_fk FOREIGN KEY (payer_id) REFERENCES tuva.payer(payer_id)
);

CREATE TABLE IF NOT EXISTS tuva.claim_line (
  claim_line_id     text PRIMARY KEY,
  claim_id          text NOT NULL,
  line_num          int,
  charge_amt        numeric,
  paid_amt          numeric,
  CONSTRAINT line_claim_fk FOREIGN KEY (claim_id) REFERENCES tuva.claim(claim_id)
);

CREATE TABLE IF NOT EXISTS tuva.diagnosis (
  claim_line_id     text,
  dx_code           text,
  dx_rank           int
);

CREATE TABLE IF NOT EXISTS tuva.procedure (
  claim_line_id     text,
  cpt_code          text,
  units             int
);
